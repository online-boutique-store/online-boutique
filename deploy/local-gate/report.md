# Local Kubernetes gate report (kind-ob-gate)

Generated 2026-10-26 from branch `feat/accounts-postgres` @ 38c36e8d (working tree dirty: all branch work is uncommitted).

## GATE RESULT: FAIL (blocking finding: shipping address in GenAI chat-span content and OTLP logs)

Nothing was pushed to ECR, no AWS call was made, and EKS and the live Observe agents were not touched. All Kubernetes
commands ran against the dedicated kubeconfig `/tmp/ob-gate/kubeconfig`, context `kind-ob-gate`, API server `https://127.0.0.1:*`.

## Images (built from the real Dockerfiles, pushed to `localhost:5001` only)
| Image | Tag | Architecture |
|---|---|---|
| 12 app images (adservice, cartservice, checkoutservice, currencyservice, emailservice, frontend, loadgenerator, paymentservice, productcatalogservice, recommendationservice, shippingservice, shoppingassistantservice) | `38c36e8d` / `38c36e8d-dirty` | linux/arm64 (host) |
| `ob-gate/fake-bedrock:local` (assistant image + tests/fakes.py) | local | linux/arm64 |
| `shoppingassistantservice:ob-gate-amd64`, `frontend:ob-gate-amd64` | local, not pushed | linux/amd64 |
| `postgres:16.4` | digest `sha256:e62fbf9d3e2b49816a32c400ed2dba83e3b361e6833e624024309c35d334b412` (multi-arch index) | arm64 in kind, amd64 in smoke |

## Manifests tested
`skaffold render` of `deploy/observe-skaffold` (40 objects) with `--build-artifacts` pointing at the local registry, plus
`deploy/accounts-db/accounts-db.yaml` applied separately. Postgres image tag was NOT yet digest-pinned in the manifest.

## Local overrides (field level)
| Object | Field | Rendered | Local | Why |
|---|---|---|---|---|
| all app Deployments | container image | bare Skaffold names | `localhost:5001/<svc>:<tag>@sha256:...` | local registry |
| shoppingassistantservice | env `BEDROCK_ENDPOINT_URL` | unset | `http://fake-bedrock.online-boutique.svc.cluster.local:8080` | no AWS |
| shoppingassistantservice | env `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | (IRSA) | `testing`/`testing` | botocore signing for the fake |
| loadgenerator | `spec.replicas` | 1 | 0 (1 only during the separate load run) | determinism |
| temporary | `PROMPT_VERSION=v2`, `RATE_LIMIT_RPM=0` on the assistant | configmap values | set for the v2 test, REMOVED afterwards | currency regression |

Cluster-side stand-ins: StorageClass `gp2` (local-path), namespace `observe` with Service `observe-agent-forwarder` backed by a local OpenTelemetry
Collector (no sampling/filter processors, file+debug exporters only), `fake-bedrock`, Calico v3.28.2 CNI (sha256 `be59408b...`), local-path provisioner
v0.0.28, local registry container, test Secrets created by running the real `deploy/observe/create-account-secrets.sh`.
`diff-overrides.py` was NOT run (PyYAML is not installed in the tooling environment); the override list was checked by hand with kubectl/jq. This is a gap.

## Results
| Check | Result |
|---|---|
| Registry pull preflight (before any app build) | PASS |
| 12 real images built (no .NET/Java stubs) | PASS |
| All app pods Ready, restarts 0, no config/pull errors | PASS |
| Required Secrets present (`shopping-assistant-account-api`, `accounts-db-credentials`) | PASS |
| Clean install Redis: 1 StatefulSet, 0 Deployments, 1 endpoint, selector `{"app":"redis-cart"}` | PASS |
| Redis pod deleted -> cart survives | PASS (carts re-read through the frontend) |
| Functional smoke through the real frontend (web signup, profile update, logout, login, guest checkout, assistant-created account, activation, replay rejected, authenticated checkout) | PASS (`LOCAL_FUNCTIONAL_SMOKE_PASS`) |
| Postgres outage (scale 0/1): assistant pod UID and restartCount unchanged | PASS |
| Postgres PVC replacement: roles recreated, registration works, assistant not restarted | PASS |
| Currency regression: v1 `JPY` accepted and order placed; v2 `Japanese Yen` rejected by the real currencyservice | PASS |
| NetworkPolicy (Calico): assistant->5432 allowed; wrong-label pod ->5432 denied; wrong-label pod ->9187 denied; `observe` pod ->9187 allowed (pod completed) | PASS |
| Loadgenerator in cluster, separate run: ~150 requests, 0 failures, returned to 0 replicas | PASS |
| Telemetry: unsampled export to the local collector, ~2,000 traces / ~12,000 spans; cross-service chain frontend -> assistant -> invoke_agent -> chat/execute_tool -> accounts.db verified | PASS (local export only) |
| Redaction scan of traces/metrics/logs against live secret values | **FAIL** (see below) |
| Scenario B Redis upgrade (reduced scope, namespace `ob-upgrade`) | PASS, 18/18 |
| amd64 smoke: python 3.12.4 x86_64, psycopg binary, readyz ready, register 201, login 200; frontend `_healthz` 200, `/login` 200 | PASS |

## Blocking finding
The scan found the plain-text chat shipping address (`1 Main St`) in `gen_ai.completion.*` and `gen_ai.prompt.*` attributes of the botocore `chat <model>` spans
(7+6+5+1 occurrences) and in the OTLP log records emitted by the Bedrock instrumentation (31 occurrences). The application's own `execute_tool` spans are
redacted (`<shipping_address_redacted>`), but the model's tool-call arguments and conversation history captured by the Bedrock instrumentation are not.
Passwords, password hashes, activation code and session hashes, the DB and account API secrets were NOT found. No code was changed.
(`73301` also matched, but it also matched metrics and is hex-ID noise, not a finding.)

## Not tested / skipped
- `diff-overrides.py` automation; the scan only covers values held in the cluster at scan time.
- Scenario B used synthetic seeded carts/sessions in Redis keys and no running cartservice/assistant writers (the writer inventory was empty), not the HEAD application images.
- The reverse-order startup subcheck (app before Postgres) and the all-namespace Calico check using the real collector labels.
- Observe ingestion, the AI Agents tab and Conversation Preview, tenant routing, Prometheus scraping, real Bedrock/Claude, IRSA, EBS, multi-node/AZ, a load balancer.

## Differences from EKS
arm64 vs probably amd64 (amd64 smoke covers frontend and assistant only); local-path vs EBS storage; Calico vs the VPC CNI policy agent; 1 control-plane + 1 worker;
no IRSA, Bedrock, Observe agent or ECR; Kubernetes v1.30.0 in kind.

## Other rollout findings
- `skaffold.yaml` sets no build platform: an arm64 Mac build will not run on amd64 nodes unless `--platform=linux/amd64` is used.
- Moving Redis from the old Deployment: label the existing pod (never the Deployment template: that restarts it and wipes `emptyDir`), isolate the standby, replicate,
  verify, cut over by Service selector; rollback is lossless only before the new instance accepts a write.
- Legacy Redis-only accounts are not migrated (`REDIS_ACCOUNT_MIGRATION_DEFERRED`, 3 synthetic keys in the simulation).

## Cluster state
Cluster `ob-gate` and registry `kind-registry` were left running for review because the gate failed. Cleanup: `deploy/local-gate/cluster-down.sh`.
