#!/usr/bin/env bash
# Blue/green deploy for the voice stack. See README.md.
#
#   deploy.sh deploy [--overlay SRC_DIR [--base IMAGE] | --context DIR] [--tag TAG] [--no-speaker-wait]
#   deploy.sh rollback
#   deploy.sh stage-up [IMAGE]      rebuild the stage stack (private DB copy) on IMAGE
#   deploy.sh stage-check           corpus + API + uniproxy handshake on both stage colors and the stage switch
#   deploy.sh stage-down
#   deploy.sh adopt [CONTAINER]     one-time move of the running single quasar container into blue/green
#   deploy.sh unit NAME [FILE]      checked restart (with rollback) of adapter|stt|ha-cli|pm
#   deploy.sh status
#
# A deploy never touches the active color or the switch until the new image has
# passed the stage checks and the idle prod color has passed its own checks.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state_dir=${AISIS_BG_STATE:-$HOME/.local/share/aisis/bluegreen-state}
app_dir=${AISIS_CODE_DIR:-$HOME/xp/aisis/services/yandex-station/code}
corpus_dir=${AISIS_CORPUS_DIR:-$HOME/.local/share/aisis/corpus}
postgres_container=${AISIS_POSTGRES_CONTAINER:-aisis-yandex-postgres-1}
speaker_wait=${AISIS_SPEAKER_WAIT_S:-90}

bg() { python3 "$here/bgctl.py" "$@"; }
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ABORT: $*"; exit 1; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

# Tells failover.py not to act while a deploy deliberately restarts things.
maintenance_on() { mkdir -p "$state_dir"; touch "$state_dir/maintenance"; trap 'rm -f "$state_dir/maintenance"' EXIT; }
maintenance_touch() { touch "$state_dir/maintenance"; }

ensure_state() {
  [[ -f "$state_dir/state.env" ]] && return 0
  local img
  img=$(docker inspect -f '{{.Config.Image}}' "${LEGACY_CONTAINER:-aisis-yandex-quasar-1}" 2>/dev/null) \
    || die "no state and no legacy container to take an image from; run: bgctl.py init IMAGE"
  bg init "$img"
}

# The same complete corpus/dialog gate runs on the frozen candidate through RunPod.
# API/handshake health checks and the subsequent flip/rollback flow stay below.
corpus_check() {
  local c=$1
  docker exec "$c" mkdir -p /src/backend/corpus || return 1
  docker cp "$corpus_dir/." "$c:/src/backend/corpus/" || return 1
  python3 "$here/corpus_runpod.py" --container "$c" --corpus-dir "$corpus_dir"
}

build_image() {
  local mode=$1 src=$2 base=$3 tag=$4 img="aisis-quasar:bg-$4" ctx
  if [[ $mode == overlay ]]; then
    [[ -d $src ]] || die "overlay dir not found: $src"
    ctx=$(mktemp -d)
    cp -a "$src" "$ctx/src"
    printf 'FROM %s\nCOPY src /src\n' "$base" >"$ctx/Dockerfile"
    docker build -q -t "$img" "$ctx" >/dev/null || { rm -rf "$ctx"; die "image build failed"; }
    rm -rf "$ctx"
  else
    docker build -q -t "$img" "$src" >/dev/null || die "image build failed"
  fi
  printf '%s' "$img"
}

stage_db_reset() {
  log "stage: copying prod database into quasar_stage"
  docker exec "$postgres_container" psql -q -U quasar -d postgres -v ON_ERROR_STOP=1 \
    -c 'DROP DATABASE IF EXISTS quasar_stage WITH (FORCE)' -c 'CREATE DATABASE quasar_stage' >/dev/null
  docker exec "$postgres_container" sh -c 'pg_dump -U quasar quasar | psql -q -U quasar -d quasar_stage -v ON_ERROR_STOP=1 >/dev/null'
}

stage_up() {
  local img=${1:-} color
  ensure_state
  [[ -n $img ]] || img=$(bg get prod "IMG_$(upper "$(bg get prod active)")")
  log "stage: image $img"
  bg compose stage rm -sf quasar-stage-blue quasar-stage-green >/dev/null 2>&1 || true
  stage_db_reset
  for color in blue green; do bg set-image stage "$color" "$img"; done
  bg render stage
  bg compose stage up -d --force-recreate >/dev/null 2>&1 || die "stage compose up failed"
  for color in blue green; do
    bg wait stage "$color" 90 || die "stage $color did not become healthy (docker logs aisis-quasar-stage-$color)"
  done
}

stage_check() {
  local color
  for color in blue green; do
    corpus_check "aisis-quasar-stage-$color" || return 1
    bg smoke stage "$color" || return 1
  done
  bg smoke stage switch || return 1
}

cmd_stage_up() { maintenance_on; stage_up "${1:-}"; log "stage is up"; }
cmd_stage_check() { stage_check && log "stage checks passed"; }
cmd_stage_down() { bg compose stage down --remove-orphans; }

# Drop old bg-* images after a successful flip: keep both prod colors' images
# (active + rollback) and anything a container still references.
prune_old_images() {
  local keep used t
  keep=" $(bg get prod IMG_BLUE) $(bg get prod IMG_GREEN) "
  used=" $(docker ps -a --format '{{.Image}}' | sort -u | tr '\n' ' ') "
  for t in $(docker images aisis-quasar --format '{{.Repository}}:{{.Tag}}' | grep ':bg-'); do
    [[ $keep == *" $t "* || $used == *" $t "* ]] && continue
    docker rmi "$t" >/dev/null 2>&1 && log "pruned old image $t"
  done
}

cmd_deploy() {
  local mode=context src="" base="" tag img active idle idle_up before
  tag=$(date +%Y%m%d-%H%M%S)
  local wait_speaker=1
  while (($#)); do
    case $1 in
      --overlay) mode=overlay; src=$2; shift 2 ;;
      --context) mode=context; src=$2; shift 2 ;;
      --base) base=$2; shift 2 ;;
      --tag) tag=$2; shift 2 ;;
      --no-speaker-wait) wait_speaker=0; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -n $src ]] || die "give --overlay SRC_DIR or --context DIR"
  maintenance_on
  ensure_state
  active=$(bg get prod active)
  idle=$([[ $active == blue ]] && echo green || echo blue)
  [[ -n $base ]] || base=$(bg get prod "IMG_$(upper "$active")")

  log "[1/6] build ($mode, base $base)"
  img=$(build_image "$mode" "$src" "$base" "$tag")
  log "image: $img"

  log "[2/6] stage: new image, private DB copy, no speaker, HA dry-run"
  stage_up "$img"
  maintenance_touch
  stage_check || die "stage checks failed on $img; prod untouched"

  log "[3/6] prod idle color: $idle"
  local prev_img
  prev_img=$(bg get prod "IMG_$(upper "$idle")")
  bg set-image prod "$idle" "$img"
  bg compose prod up -d --no-deps --force-recreate "quasar-prod-$idle" >/dev/null 2>&1 \
    || { bg set-image prod "$idle" "$prev_img"; die "could not start prod $idle"; }
  if ! bg wait prod "$idle" 90 || ! corpus_check "aisis-quasar-prod-$idle" || ! bg smoke prod "$idle"; then
    log "idle $idle failed its checks; restoring previous image on it"
    bg set-image prod "$idle" "$prev_img"
    bg compose prod up -d --no-deps --force-recreate "quasar-prod-$idle" >/dev/null 2>&1 || true
    die "prod $idle failed pre-flip checks; $active still serving"
  fi
  maintenance_touch

  before=$(bg devices prod || true)
  log "[4/6] flip $active -> $idle (connected before: ${before:-none})"
  bg flip prod "$idle" >&2

  log "[5/6] verify through the switch"
  if ! bg smoke prod switch >/dev/null; then
    bg flip prod "$active" >&2 || true
    die "post-flip smoke failed; rolled back to $active"
  fi
  if [[ $wait_speaker == 1 && -n $before ]]; then
    local waited=0
    until [[ -n $(bg devices prod || true) ]]; do
      ((waited++ >= speaker_wait)) && { bg flip prod "$active" >&2 || true; die "speaker did not reconnect to $idle within ${speaker_wait}s; rolled back to $active"; }
      sleep 1
    done
    log "speaker reconnected on $idle after ~${waited}s"
  fi

  log "[6/6] done: $idle is active on $img; $active stays warm for rollback (image $(bg get prod "IMG_$(upper "$active")"))"
  prune_old_images || true
}

cmd_rollback() {
  local active standby
  maintenance_on
  active=$(bg get prod active); standby=$(bg get prod standby)
  bg health prod "$standby" >/dev/null || die "standby $standby is not healthy; not flipping"
  bg flip prod "$standby" >&2
  bg smoke prod switch >/dev/null || { bg flip prod "$active" >&2; die "post-rollback smoke failed; flipped back"; }
  log "rolled back: $standby is active"
}

# adopt: move the single legacy container onto blue/green. Downtime is only the gap
# between stopping the legacy container and the switch binding the canonical ports.
cmd_adopt() {
  local legacy=${1:-aisis-yandex-quasar-1} img before
  maintenance_on
  img=$(docker inspect -f '{{.Config.Image}}' "$legacy") || die "legacy container $legacy not found"
  bg init "$img" --force
  bg render prod
  log "starting blue and green on $img next to the legacy container"
  bg compose prod up -d quasar-prod-blue quasar-prod-green >/dev/null 2>&1 || die "colors failed to start"
  for color in blue green; do
    bg wait prod "$color" 90 || die "$color unhealthy"
    corpus_check "aisis-quasar-prod-$color" || die "corpus failed on $color"
    bg smoke prod "$color" || die "smoke failed on $color"
  done
  before=$(curl -s -m3 http://127.0.0.1:31116/devices | grep -c '"connected":true' || true)
  log "cutover: legacy container -> switch (speaker connected before: $before)"
  docker update --restart=no "$legacy" >/dev/null
  docker stop -t 2 "$legacy" >/dev/null
  bg compose prod up -d switch-prod >/dev/null 2>&1 || {
    log "switch failed to start; reviving legacy container"; docker update --restart=unless-stopped "$legacy" >/dev/null; docker start "$legacy" >/dev/null
    die "switch failed"; }
  bg smoke prod switch >/dev/null || die "switch smoke failed (legacy container $legacy kept stopped for manual revive)"
  if ((before > 0)); then
    local waited=0
    until [[ -n $(bg devices prod || true) ]]; do
      ((waited++ >= speaker_wait)) && die "speaker has not reconnected after ${speaker_wait}s"
      sleep 1
    done
    log "speaker reconnected on blue after ~${waited}s"
  fi
  log "adopted; legacy container $legacy is stopped and kept (docker rm $legacy when no longer needed)"
}

# unit NAME [FILE]: back up, optionally replace the installed file, restart, wait for health, else restore.
cmd_unit() {
  local name=${1:?unit name} newfile=${2:-} file unit url body
  case $name in
    adapter) file=adapter.py unit=aisis-station-adapter.service url=http://127.0.0.1:31117/healthz ;;
    stt)     file=stt_wyoming.py unit=aisis-station-stt.service url=http://127.0.0.1:31121/healthz ;;
    ha-cli)  file=hacli.py unit=aisis-ha-cli.service url=http://127.0.0.1:8795/health ;;
    pm)      file=pm_cdp_server.py unit=aisis-station-pm.service url=http://127.0.0.1:31118/health ;;
    *) die "unknown unit $name (adapter|stt|ha-cli|pm)" ;;
  esac
  maintenance_on
  local target=$app_dir/$file backup=$state_dir/unit-backups/$file.$(date +%Y%m%d-%H%M%S)
  mkdir -p "$state_dir/unit-backups"
  cp -p "$target" "$backup"
  if [[ -n $newfile ]]; then
    python3 -m py_compile "$newfile" || die "$newfile does not compile"
    install -m "$(stat -c %a "$target")" "$newfile" "$target"
  fi
  systemctl --user restart "$unit"
  local i
  for i in $(seq 1 30); do
    if curl -fsS -m2 "$url" >/dev/null 2>&1; then log "$name healthy after ${i}s"; return 0; fi
    sleep 1
  done
  log "$name unhealthy after restart; restoring $backup"
  install -m "$(stat -c %a "$backup")" "$backup" "$target"
  systemctl --user restart "$unit"
  die "$name rolled back to the previous file"
}

cmd_status() { bg status; }

cmd=${1:-}
shift || true
case $cmd in
  deploy) cmd_deploy "$@" ;;
  rollback) cmd_rollback ;;
  stage-up) cmd_stage_up "$@" ;;
  stage-check) cmd_stage_check ;;
  stage-down) cmd_stage_down ;;
  adopt) cmd_adopt "$@" ;;
  unit) cmd_unit "$@" ;;
  status) cmd_status ;;
  *) sed -n '2,15p' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
esac
