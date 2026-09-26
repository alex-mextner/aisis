#!/usr/bin/env bash
# Proves that .gitleaks.toml loads and its rules fire: scans a throwaway directory holding random
# tailnet names of several shapes and a random AWS-style key, and requires a tailnet-hostname
# finding for every name plus the default aws-access-token rule. Exceptions belong in
# .gitleaksignore (exact fingerprints), so .gitleaks.toml may not declare an allowlist.
# The config itself is reviewed code: this catches a config that does not load, or a rule narrowed
# or allowlisted by mistake, not a deliberately weakened config together with this script.
#
# Usage: scripts/gitleaks-probe.sh <gitleaks image> [repository directory, default .]
set -euo pipefail

image=$1
repo=$(cd "${2:-.}" && pwd)

if grep -qiE '^[[:space:]]*\[+[[:space:]]*([a-z]+\.)*allowlists?[[:space:]]*\]+' "$repo/.gitleaks.toml"; then
  echo "::error::.gitleaks.toml declares an allowlist; add exceptions to .gitleaksignore instead" >&2
  exit 1
fi

probe=$(mktemp -d)
trap 'rm -rf "$probe"' EXIT
mkdir -p "$probe/docs/specs"
hex() { openssl rand -hex "$1"; }
# Generated at run time, so this script itself contains no tailnet name or key.
suffix=ts.net
names=(
  "home.tail$(hex 3).$suffix"             # host in a generated tailnet name
  "tail$(hex 3).$suffix"                  # bare tailnet domain
  "nas-$(hex 2).$(hex 3)-$(hex 2).$suffix" # host in a custom tailnet name
)
{
  for name in "${names[@]}"; do printf 'http://%s:8123\n' "$name"; done
  printf 'aws_access_key_id = AKIA%s\n' "$(openssl rand 10 | base32)"
} > "$probe/docs/specs/probe.md"

# Leaks exit with 3, so a gitleaks error (1) or a docker failure (125 and up) is told apart.
rc=0
docker run --rm -v "$repo:/repo:ro" -v "$probe:/probe" "$image" \
  dir --config /repo/.gitleaks.toml --exit-code 3 --redact --no-banner \
  --report-format json --report-path /probe/report.json /probe/docs || rc=$?
case "$rc" in
  3) ;;
  0) echo "::error::gitleaks found nothing in the probe: .gitleaks.toml rules are not active" >&2; exit 1 ;;
  1) echo "::error::gitleaks failed on the probe (config or runtime error, see above)" >&2; exit 1 ;;
  *) echo "::error::docker could not run gitleaks (exit $rc)" >&2; exit 1 ;;
esac

fired=$(jq '[.[] | select(.RuleID == "tailnet-hostname")] | length' "$probe/report.json")
if [ "$fired" -lt "${#names[@]}" ]; then
  echo "::error::tailnet-hostname fired on $fired of ${#names[@]} probe names" >&2
  exit 1
fi
if ! jq -e 'any(.[]; .RuleID == "aws-access-token")' "$probe/report.json" > /dev/null; then
  echo "::error::default rule aws-access-token did not fire; is useDefault = true?" >&2
  exit 1
fi
echo "gitleaks probe ok: tailnet-hostname fired on ${#names[@]} names; default rules active"
