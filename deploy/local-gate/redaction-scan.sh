#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
N=online-boutique
sec() { k -n $N get secret "$1" -o jsonpath="{.data.$2}" | base64 -d; }
DBPW=$(sec accounts-db-credentials APP_DB_PASSWORD)
SUPW=$(sec accounts-db-credentials POSTGRES_PASSWORD)
API=$(sec shopping-assistant-account-api ACCOUNT_API_SECRET)
q() { k -n $N exec accounts-db-0 -c postgres -- env PGPASSWORD="$SUPW" psql -h 127.0.0.1 -U postgres -d accounts -Atc "$1"; }
HASHES=$(q "select string_agg(password_hash,'|') from customers where password_hash is not null")
CODES=$(q "select coalesce(string_agg(code_hash||'|'||session_id_hash,'|'),'') from activation_claims")
export LOCAL_GATE_REDACTION_VALUES="Sup3rPassw0rd!|Other1Passw0rd|$DBPW|$SUPW|$API|$HASHES|$CODES|1 Main St|2 Main St|Dallas|73301"
python3 "$SCRIPT_DIR/lib/check_telemetry.py" /tmp/ob-gate/telemetry/traces.json /tmp/ob-gate/telemetry/metrics.json /tmp/ob-gate/telemetry/logs.json
echo "values_scanned=$(echo "$LOCAL_GATE_REDACTION_VALUES" | tr '|' '\n' | grep -c .)"
