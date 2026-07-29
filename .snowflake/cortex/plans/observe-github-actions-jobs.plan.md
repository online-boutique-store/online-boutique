# Plan: Observe GitHub Actions — Continuous Data Generation

## Goal

Keep the Observe by Snowflake demo environment populated with rich, realistic telemetry at all times: traces, logs, metrics, deployment markers, incident arcs, and CI/CD events. Every job targets the EKS cluster using AWS credentials stored as GitHub Secrets and `kubectl` via the `aws eks update-kubeconfig` pattern.

---

## Overview of the six jobs

### 1. `observe-load-generator.yaml` — Heartbeat traffic (every 30 min)

**Trigger:** `schedule: '*/30 * * * *'` + `workflow_dispatch`

The most fundamental job. Without continuous traffic there are no traces, logs, or metrics to show. This job ensures the Locust load generator deployment is running and healthy.

**Steps:**
1. Configure AWS credentials + EKS kubeconfig
2. Check if `loadgenerator` deployment exists; create it from `deploy/observe/loadgenerator.yaml` if not
3. Verify pod is `Ready` — restart if crash-looped
4. Log aggregate request/error counts from the load generator pod

**Why it matters:** Every other Observe feature (service map, traces, log correlation, error rate charts) depends on traffic flowing. This is the heartbeat.

**Variables needed:** `USERS` (default 5), `EKS_CLUSTER_NAME`, `AWS_REGION`

---

### 2. `observe-incident-cycle.yaml` — Break/fix arc (every 4 hours)

**Trigger:** `schedule: '0 */4 * * *'` + `workflow_dispatch` with a `mode` input (`cycle` / `break-only` / `fix-only`)

The core demo job. Runs `demo-break.sh`, waits 8 minutes for errors to accumulate in Observe, then runs `demo-fix.sh`. This creates the full incident arc (error spike → deployment marker → recovery) that is the centerpiece of the Observe SE demo.

**Steps:**
1. Configure AWS credentials + EKS kubeconfig
2. Confirm load generator is running (fail fast if not — no point breaking without traffic)
3. Run `deploy/observe/demo-break.sh` (injects wrong Redis port, bumps `service.version` to `1.0.3-broken-redis`)
4. Sleep 8 minutes (gives Observe time to accumulate traces, errors, and log data)
5. Run `deploy/observe/demo-fix.sh` (restores Redis, bumps to `1.0.4-fixed-redis`)
6. Wait for rollouts, verify `REDIS_ADDR` is restored

**Schedule math:** Running at `0 0,4,8,12,16,20 * * *` means there is always an incident + recovery within the last 4 hours — ideal for ad-hoc demos at any time of day.

**Manual override:** `workflow_dispatch` with `mode: break-only` lets a demo SE inject the incident immediately before walking into a call, and `mode: fix-only` lets them restore after.

**Why it matters:** The incident arc (deployment marker → error spike → trace → log correlation → fix) is the central 20 minutes of the demo flow. Without a recent incident in the account there is nothing interesting to show.

---

### 3. `observe-deployment-pulse.yaml` — Routine deployment markers (every 2 hours)

**Trigger:** `schedule: '0 */2 * * *'` + push to `main`

Between incident cycles, this job bumps `OTEL_RESOURCE_ATTRIBUTES` (specifically `service.version`) on `frontend`, `checkoutservice`, and `productcatalogservice` with a timestamp-based version string (e.g. `1.0.5-2026-07-29-14`). This creates routine "green" deployment markers in Observe APM so the bad deployment in the incident cycle stands out by contrast.

**Steps:**
1. Configure AWS credentials + EKS kubeconfig
2. Generate a version string: `1.0.x-$(date +%Y-%m-%d-%H)` (a new patch each run)
3. `kubectl set env` on the three services with the new `OTEL_RESOURCE_ATTRIBUTES`
4. Wait for rollouts on all three

**Why it matters:** The demo story says "your team ships multiple times a day." Regular green deployment markers make the demo account look like a real production environment, and make the bad deployment event visually stand out in the APM timeline.

---

### 4. `observe-unit-tests-report.yaml` — CI/CD signals (hourly)

**Trigger:** `schedule: '5 * * * *'` + push to `main`

Runs the existing Go and C# unit tests, then POSTs a structured event to Observe's HTTP ingest endpoint summarizing the results (pass count, fail count, duration, commit SHA). This populates the Observe account with CI/CD event data alongside APM data.

**Steps:**
1. `actions/setup-go` + `actions/setup-dotnet`
2. Run Go tests (`shippingservice`, `productcatalogservice`, `checkoutservice/money`, `frontend`) with `-json` output; parse pass/fail counts
3. Run C# tests with `dotnet test --logger trx`; parse results
4. POST event to Observe HTTP ingest: `{"type":"ci_test_run","commit":"$SHA","go_pass":N,"go_fail":0,"cs_pass":N,"duration_s":X}`
5. Fail the workflow if any tests fail (keeps the CI signal honest)

**Why it matters:** Shows the "Snowflake-native data model" story — CI/CD events live in the same Observe account as APM traces, making it possible to correlate a test failure with a subsequent deployment event.

**New secret needed:** `OBSERVE_INGEST_TOKEN`, `OBSERVE_CUSTOMER_ID`

---

### 5. `observe-synthetic-canary.yaml` — External availability (every 5 min)

**Trigger:** `schedule: '*/5 * * * *'`

Hits the frontend LoadBalancer URL from a GitHub-hosted runner (simulating an external user), measures HTTP response time and status, then sends a synthetic span to the Observe OTLP HTTP endpoint. This generates an external "availability" signal that is distinct from the in-cluster Locust traffic.

**Steps:**
1. Record `start_time`
2. `curl -o /dev/null -sw "%{http_code} %{time_total}"` against `$FRONTEND_LB_URL`
3. Build a minimal OTLP JSON payload (`ResourceSpans` with one span: `synthetic.http.check`, attributes: `http.status_code`, `http.url`, `http.response_time_ms`, `canary.region: github-actions`)
4. POST to `https://$OBSERVE_CUSTOMER_ID.collect.observeinc.com/v1/traces`

**Why it matters:** Gives a second traffic source in the service map (external vs. in-cluster), and provides a simple SLA view: if the frontend is down, the canary fails immediately even when the load generator is between runs. Also generates `http.response_time_ms` as a metric visible in Observe.

**New secrets needed:** `FRONTEND_LB_URL`, `OBSERVE_INGEST_TOKEN`, `OBSERVE_CUSTOMER_ID`

---

### 6. `observe-traffic-ramp.yaml` — Business-hours traffic patterns (weekday peaks)

**Trigger:** `schedule: '0 9,14,17 * * 1-5'` (Mon–Fri at 09:00, 14:00, 17:00 UTC)

Temporarily scales the in-cluster load generator to 20 concurrent users for 20 minutes, then scales back to 5. This creates realistic "business hours" traffic peaks and valleys in the Observe metrics timeline, making request rate and latency charts more visually interesting for demos.

**Steps:**
1. Configure AWS credentials + EKS kubeconfig
2. `kubectl scale deployment/loadgenerator --replicas=4` (4 pods × 5 users default = 20 concurrent)
3. Sleep 20 minutes
4. `kubectl scale deployment/loadgenerator --replicas=1` (back to baseline)
5. Log final request stats

**Alternative if scaling replicas is not clean:** re-deploy `loadgenerator.yaml` with `USERS=20`, then re-deploy with `USERS=5`.

**Why it matters:** Flat traffic is less interesting to demo. A dashboard with visible peaks (matching the "multiple deploys per day" story) looks like a real production environment.

---

## Required GitHub Secrets

| Secret | Used by | Description |
|--------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | jobs 1–3, 6 | IAM user key with EKS read + kubectl exec permissions |
| `AWS_SECRET_ACCESS_KEY` | jobs 1–3, 6 | Matching secret key |
| `AWS_REGION` | jobs 1–3, 6 | e.g. `us-east-1` |
| `EKS_CLUSTER_NAME` | jobs 1–3, 6 | e.g. `online-boutique` |
| `OBSERVE_CUSTOMER_ID` | jobs 4–5 | Observe tenant ID (from Observe UI → Settings → Data Ingestion) |
| `OBSERVE_INGEST_TOKEN` | jobs 4–5 | Observe HTTP ingest token |
| `FRONTEND_LB_URL` | job 5 | External LoadBalancer URL, e.g. `http://a1b2c3.us-east-1.elb.amazonaws.com` |

> **IAM note:** The AWS credentials only need `eks:DescribeCluster` (to update kubeconfig) plus whatever kubectl RBAC is configured on the cluster. Prefer OIDC (`AWS_ROLE_TO_ASSUME`) over long-lived keys if your AWS account supports it.

---

## Runner strategy

Jobs 1–3 and 6 (EKS-targeting) use `ubuntu-latest` (GitHub-hosted) with:
```yaml
- uses: aws-actions/configure-aws-credentials@v4
- run: aws eks update-kubeconfig --name $EKS_CLUSTER_NAME --region $AWS_REGION
```

Jobs 4–5 (test running + HTTP calls) also use `ubuntu-latest` with no Kubernetes dependency.

The existing `[self-hosted, is-enabled]` runner for `ci-main.yaml` / `ci-pr.yaml` is left untouched — those target GKE for code review CI, while these new workflows target EKS for Observe demo data generation.

---

## Data flow summary

```
GitHub Actions (every 5 min)  ──HTTP OTLP──►  Observe (synthetic spans)
GitHub Actions (every 30 min) ──kubectl──►  loadgenerator pod ──OTLP──►  OTEL collector ──►  Observe
GitHub Actions (every 2 hr)   ──kubectl──►  service.version bump ──►  Observe deployment markers
GitHub Actions (every 4 hr)   ──kubectl──►  demo-break/fix ──►  Observe incident arc
GitHub Actions (hourly)       ──curl HTTP──►  Observe custom events (CI test results)
GitHub Actions (weekday 3×)   ──kubectl──►  loadgenerator scale ──►  traffic peaks in Observe
```
