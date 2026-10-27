#!/usr/bin/env bash
# Runs the whole accountservice local test suite. Needs: python venv with the service requirements plus
# pgserver, pyyaml; redis-server on PATH. No Docker, no cluster, no network.
set -uo pipefail
PY="${PY:-/tmp/obvenv311/bin/python}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
fail=0
export PATH=/opt/homebrew/bin:/usr/local/go/bin:$PATH
(cd "$HERE/../frontend" && go build -o /tmp/fe_bin . && go test -count=1 ./... > /tmp/frontend_go_test.log 2>&1) || { echo "FAILED: frontend build/go test (see /tmp/frontend_go_test.log)"; fail=1; }
for t in test_validation test_migrations test_accountservice test_sanitizer_layers test_frontend_e2e test_loadgen_identities test_loadgen_customer test_payment_methods test_checkout_durability; do
  echo "== $t"
  if ! "$PY" "tests/$t.py" > "/tmp/accountservice_$t.log" 2>&1; then
    echo "FAILED: $t (see /tmp/accountservice_$t.log)"; grep -E "^FAIL" "/tmp/accountservice_$t.log" | head; fail=1
  else
    echo "  $(grep -c '^PASS' "/tmp/accountservice_$t.log") checks passed"
  fi
done
for t in test_checkout test_account_tools test_account_grpc test_telemetry_redaction test_bedrock_redaction test_agent_local test_accounts_pg; do
  echo "== assistant/$t"
  if ! (cd "$HERE/../shoppingassistantservice" && "$PY" "tests/$t.py" > "/tmp/assistant_$t.log" 2>&1); then
    echo "FAILED: assistant/$t (see /tmp/assistant_$t.log)"; grep -E "^FAIL|Error" "/tmp/assistant_$t.log" | head -5; fail=1
  else
    echo "  ok"
  fi
done
exit $fail
