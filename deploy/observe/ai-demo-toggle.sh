#!/usr/bin/env bash
# Live toggles for the AI shopping-assistant incident (no rebuild, no commit).
#
#   ./deploy/observe/ai-demo-toggle.sh status
#   ./deploy/observe/ai-demo-toggle.sh v2            # prompt regression ON  (model decides the args)
#   ./deploy/observe/ai-demo-toggle.sh v1            # healthy prompts
#   ./deploy/observe/ai-demo-toggle.sh inject-on     # SYNTHETIC fault: app rewrites currency_code
#   ./deploy/observe/ai-demo-toggle.sh inject-off
#
# `v2` is a genuine prompt change: whether the model then emits an invalid
# currency is the model's own behaviour. `inject-on` is NOT: the service rewrites
# the model's argument after the fact and labels it fault.injected=true. Use it
# only if v2 alone doesn't reproduce reliably, and say so when demoing.
#
# `kubectl set env` overrides the generated ConfigMap value and rolls the pod.
# A later `skaffold run` leaves these overrides in place; clear one with
# `kubectl set env deploy/shoppingassistantservice PROMPT_VERSION-`.
# Not exercised by CI; review before first use.
set -euo pipefail

NS="${NAMESPACE:-online-boutique}"
DEP="deploy/shoppingassistantservice"

case "${1:-}" in
  v1|v2)      kubectl -n "$NS" set env "$DEP" PROMPT_VERSION="$1" ;;
  inject-on)  kubectl -n "$NS" set env "$DEP" FAULT_INJECTION=force_malformed_currency ;;
  inject-off) kubectl -n "$NS" set env "$DEP" FAULT_INJECTION= ;;
  status)     kubectl -n "$NS" set env "$DEP" --list | grep -E '^(PROMPT_VERSION|FAULT_INJECTION|BEDROCK_MODEL_ID|AWS_REGION)=' || true
              kubectl -n "$NS" get cm shopping-assistant-config -o jsonpath='{.data}' ; echo ;;
  *) echo "usage: $0 {v1|v2|inject-on|inject-off|status}" >&2; exit 2 ;;
esac
[[ "${1}" == status ]] || kubectl -n "$NS" rollout status "$DEP" --timeout=120s
