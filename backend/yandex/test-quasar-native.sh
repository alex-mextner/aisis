#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=${AISIS_QUASAR_SOURCE:?set absolute path to patched Quasar source}
image=${AISIS_QUASAR_IMAGE:-aisis-quasar:a983cda}
[[ -f "$source_dir/src/routers/uniproxy/voice-input-handler.ts" ]] || exit 1
if [[ ${1:-} == --live ]]; then
  fixture=${AISIS_SPEECH_FIXTURE:?set path to synthetic Bracho Ogg speech fixture}
  [[ -f "$fixture" ]] || exit 1
  docker run --rm --network host --read-only --tmpfs /tmp \
    -v "$source_dir/src:/src:ro" \
    -v "$script_dir/quasar-native-live.test.ts:/aisis-native-live.test.ts:ro" \
    -v "$fixture:/aisis-probe.ogg:ro" \
    "$image" node --import tsx --test /aisis-native-live.test.ts
else
  docker run --rm --network none --read-only --tmpfs /tmp \
    -v "$source_dir/src:/src:ro" \
    -v "$script_dir/quasar-native-eof.test.ts:/aisis-native-eof.test.ts:ro" \
    -v "$script_dir/quasar-ogg-packets.test.ts:/quasar-ogg-packets.test.ts:ro" \
    "$image" node --import tsx --test /aisis-native-eof.test.ts /quasar-ogg-packets.test.ts
fi
