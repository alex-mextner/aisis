#!/usr/bin/env bash
# Proves that .gitleaks.toml loads and its rules fire: scans a throwaway directory holding random
# tailnet names of several shapes and a random AWS-style key, and requires a tailnet-hostname
# finding for every name plus the default aws-access-token rule. Exceptions belong in
# .gitleaksignore (exact fingerprints), so .gitleaks.toml may not declare an allowlist or disable
# default rules. The config itself is reviewed code: this catches a config that does not load, or
# a rule narrowed, allowlisted or disabled by mistake, not a deliberately weakened config together
# with this script.
#
# Usage: scripts/gitleaks-probe.sh <gitleaks image> [repository directory, default .]
set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "usage: $0 <gitleaks image> [repository directory, default .]" >&2
  exit 2
fi
image=$1
repo=$(cd "${2:-.}" && pwd)

# Any allowlist (table, array of tables or inline key, global or per rule) and any disabled default
# rule is refused: outside comments, the words may not appear in the config at all.
if grep -vE '^[[:space:]]*#' "$repo/.gitleaks.toml" | grep -qiE 'allowlist|disabledrules'; then
  echo "::error::.gitleaks.toml declares an allowlist or disables rules; add exceptions to .gitleaksignore instead" >&2
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
# 16 characters of the base32 alphabet the default aws-access-token rule expects. Only openssl,
# tr and cut (all on stock macOS and Linux), and no reader that exits early under pipefail.
key=$(openssl rand -base64 96 | LC_ALL=C tr -dc 'A-Z2-7' | cut -c1-16)
[ "${#key}" -eq 16 ] || { echo "::error::could not generate a probe key" >&2; exit 1; }
{
  for name in "${names[@]}"; do printf 'http://%s:8123\n' "$name"; done
  printf 'aws_access_key_id = AKIA%s\n' "$key"
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
