#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"

fingerprint() {
  local pod="$1"
  k -n "$GATE_NAMESPACE" exec "$pod" -- redis-cli --raw --scan | sort | while read -r key; do
    printf '%s ' "$key"
    k -n "$GATE_NAMESPACE" exec "$pod" -- redis-cli --raw dump "$key" | shasum -a 256 | cut -d' ' -f1
    k -n "$GATE_NAMESPACE" exec "$pod" -- redis-cli --raw ttl "$key"
  done
}

old_pod="${1:?old pod required}"
standby_pod="${2:?standby pod required}"
old_file="/tmp/ob-gate/redis-old.fingerprint"
new_file="/tmp/ob-gate/redis-standby.fingerprint"
fingerprint "$old_pod" > "$old_file"
fingerprint "$standby_pod" > "$new_file"
if cmp -s "$old_file" "$new_file"; then
  printf 'REDIS_FINGERPRINT_MATCH\n'
else
  printf 'REDIS_FINGERPRINT_MISMATCH\n' >&2
  diff -u "$old_file" "$new_file" || true
  exit 1
fi
