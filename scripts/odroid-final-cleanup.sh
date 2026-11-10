#!/usr/bin/env bash
# Final odroid cleanup after the voice experiment wind-down (Refs #74).
# Run ON the odroid as user ultra. Dry-run by default; pass --apply to delete.
# Idempotent: every step checks that its target exists first. No secrets.
set -u

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
echo "mode: $([ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN)"

HA=/home/ultra/homeassistant

step() { echo "-- $*"; }
run() {
  if [ "$APPLY" = 1 ]; then
    echo "   run: $*"
    "$@" || echo "   FAILED (continuing): $*"
  else
    echo "   would run: $*"
  fi
}

# 1. merged voice-winddown branch (already an ancestor of main)
step "git branch voice-winddown"
if git -C "$HA" show-ref --verify --quiet refs/heads/voice-winddown; then
  run git -C "$HA" branch -d voice-winddown
else
  echo "   absent"
fi

# 2. leftover voice dirs/files (root-owned __pycache__ may need a failure to be ignored)
for d in \
  /home/ultra/homeassistant/custom_components/wakeword_studio \
  /home/ultra/odroid-overlay/wyoming-satellite; do
  step "dir $d"
  if [ -e "$d" ]; then run rm -r "$d"; else echo "   absent"; fi
done

for f in \
  /home/ultra/odroid-overlay/wyoming-satellite/Dockerfile \
  /home/ultra/odroid-overlay/backups-20261011/packages.bak-voice_assist_monitor.yaml-20261011-haodroid \
  /home/ultra/odroid-overlay/backups-20261011/packages.bak-voice_conversation_log.yaml-20261011-haodroid; do
  step "file $f"
  if [ -e "$f" ]; then run rm "$f"; else echo "   absent"; fi
done

# 3. dangling docker images left by old builds (running containers are unaffected)
step "docker dangling images"
echo "   count: $(docker images -q --filter dangling=true | wc -l)"
run docker image prune -f

# 4. unused docker volumes (anonymous, not attached to any container)
step "docker unused volumes"
echo "   count: $(docker volume ls -q --filter dangling=true | wc -l)"
run docker volume prune -f

echo "done ($([ "$APPLY" = 1 ] && echo applied || echo "dry-run; rerun with --apply"))"
