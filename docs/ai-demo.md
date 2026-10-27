# AI Shopping Assistant Demo

## What This Demonstrates

The shopping assistant is a Python Flask agent that calls Claude on **Amazon Bedrock** through the Converse API. It runs real tool calls against the existing Online Boutique gRPC services. The default model is the US cross-region inference profile `us.anthropic.claude-sonnet-4-6`. It is listed ACTIVE in account `739672403694`; the older `claude-sonnet-4-5` foundation model there is LEGACY.

A trace runs:

`frontend /bot` -> `invoke_agent shopping_assistant` -> `chat <model>` -> `execute_tool <name>` -> Online Boutique gRPC services

The tools are `search_products`, `get_product`, `convert_price`, `add_to_cart`, `get_cart`, `start_checkout`, `lookup_account`, `create_account`, `continue_as_guest`, `set_shipping_address`, `list_payment_methods`, `set_payment`, `set_currency`, `review_order`, `confirm_order`, and `place_order`.

## AWS Setup (needs approval: creates IAM resources)

The pod uses **no static AWS keys**. It gets credentials from its Kubernetes ServiceAccount `shoppingassistantservice` through the standard AWS chain.

1. Confirm which models your account can use, and pick a model or inference-profile ID:

   ```bash
   aws bedrock list-inference-profiles --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `claude`)].inferenceProfileId'
   ```

   If it isn't the default, set `BEDROCK_MODEL_ID` in `deploy/observe-skaffold/kustomization.yaml`. Also set `AWS_REGION` to the region where you invoke Bedrock.

2. Create an IAM role that holds `deploy/observe/bedrock-invoke-policy.json`. Replace `<ACCOUNT_ID>` in that file, and update the model ARNs if you changed models.

3. Bind the role to the pod. Use one of these two options:
   - **EKS Pod Identity** (preferred). The cluster must have the `eks-pod-identity-agent` add-on.

     ```bash
     aws eks create-pod-identity-association --cluster-name <cluster> \
       --namespace <namespace> --service-account shoppingassistantservice \
       --role-arn arn:aws:iam::<ACCOUNT_ID>:role/<bedrock-invoke-role>
     ```

     The role's trust policy must allow `pods.eks.amazonaws.com`.

   - **IRSA**. Uncomment the `eks.amazonaws.com/role-arn` annotation on the ServiceAccount in `deploy/observe-skaffold/shoppingassistantservice.yaml`. The role trusts the cluster's OIDC provider.

4. EKS egress must reach `bedrock-runtime.<region>.amazonaws.com`, either over NAT or through a VPC endpoint.

## Configuration

These values live in the generated `shopping-assistant-config` ConfigMap:

- `BEDROCK_MODEL_ID`
- `AWS_REGION`
- `PROMPT_VERSION=v1`
- `FAULT_INJECTION=`
- `MAX_TOOL_STEPS=5`
- `MAX_COMPLETION_TOKENS=1024`
- `RATE_LIMIT_RPM=30`
- `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`

Content capture must be the literal `true`. I verified that the botocore Bedrock instrumentation ignores `SPAN_ONLY`. With `true`, prompts and responses are recorded in two places:

- **On the `chat` span** as OTel semconv `gen_ai.input.messages` and `gen_ai.output.messages`, including tool-call names and arguments. The service adds these through botocore hooks, because the official instrumentation does not put content on spans. It also emits indexed `gen_ai.prompt.N.*` and `gen_ai.completion.N.*` attributes for Observe views that consume the legacy text shape.
- **On the `invoke_agent` span** as the same standard `gen_ai.input.messages` and `gen_ai.output.messages` attributes, plus indexed `gen_ai.prompt.N.*` and `gen_ai.completion.N.*` attributes. `app.prompt.text` remains as a compatibility attribute for existing dashboards.
- **As OTel log events** named `gen_ai.user.message`, `gen_ai.choice`, and so on. The official instrumentation emits these.

## Deploy (needs approval)

```bash
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com
echo "OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production,deployment.environment.name=production,service.version=$(git rev-parse --short HEAD)" \
  > deploy/observe-skaffold/service-version.env
skaffold run --default-repo=<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/online-boutique --namespace=<namespace>
```

`ai-demo-toggle.sh` defaults to the `online-boutique` namespace, so pass `NAMESPACE=<namespace>` if yours differs.

## Guided Checkout And Observability

The checkout is conversational, but its correctness is enforced by a Python state machine. The customer starts with `I'd like to check out.` The agent then identifies the customer, looks up or creates an account, collects a shipping address when needed, offers synthetic payment methods, asks for currency, reads a review, waits for an explicit confirmation turn, and only then calls `place_order`. Payment IDs expose only labels and last four digits; card numbers never enter prompts, tool results, or telemetry.

Accounts live in PostgreSQL (`deploy/accounts-db/accounts-db.yaml`). Redis keeps only sessions, login lockouts and a 15-minute activation handoff. See [Customer accounts on Postgres](#customer-accounts-on-postgres).

The trace shape is:

```text
frontend /bot
  invoke_agent shopping_assistant                  traceloop.span.kind=agent
    chat <Bedrock model>                           traceloop.span.kind=task, llm.request.type=chat
    execute_tool lookup_account                    traceloop.span.kind=tool
      account.lookup                              OpenTelemetry child span
        accounts.db.customer.lookup               business span
          SELECT ... FROM customers               psycopg CLIENT span (parameterized)
    execute_tool create_account
      account.create
        accounts.db.claim.create -> INSERT customers / addresses / activation_claims
    execute_tool set_payment / set_currency
    execute_tool review_order / confirm_order
    execute_tool place_order
      CheckoutService/PlaceOrder -> CurrencyService/Convert
```

The shared correlation fields are `app.session.id`, `app.customer.id`, `app.conversation.id`, `app.customer.email_hash`, `app.customer.type`, `app.checkout.id`, `app.checkout.stage`, and `app.prompt.version`. The agent preserves `traceloop.span.kind`, `llm.request.model`, `llm.response.model`, `llm.request.type`, `gen_ai.*` token/model attributes, tool names, and prompt version attributes required by Observe AI Agents.

Checkout business telemetry is emitted through OpenTelemetry metrics and correlated structured OTLP logs:

- `ai.checkout.funnel.stage` counts each session's first entry into a stage once; `prefilled=true` identifies returning-customer data.
- `ai.checkout.terminal` records exactly one `completed`, `failed`, or `abandoned` outcome per attempt.
- `ai.checkout.attempts`, `ai.checkout.value_usd`, `ai.checkout.retries`, and `ai.checkout.recovered_value_usd` measure attempts, value at risk, retries, and recovered value.
- `ai.account.lookups`, `ai.account.creations`, and `ai.checkout.currency_selections` measure account and currency behavior.
- `ai.assistant.request.duration`, `ai.llm.call.duration`, and `ai.tool.duration` measure end-to-end, Bedrock, and tool latency.
- Events include `account.lookup`, `account.create`, `checkout.stage`, `checkout.currency_selected`, `checkout.review`, `checkout.confirmed`, `checkout.terminal`, and `checkout.attempt`.

To investigate the prompt regression, filter traces on `prompt.version=v2` and `app.tool.currency_valid=false`. Start at the failed `execute_tool place_order` span, follow its `CheckoutService/PlaceOrder` child to `CurrencyService/Convert`, then inspect the same trace's earlier `chat` span and `gen_ai.output.messages` tool call. The v2 path should show the model-provided `Japanese Yen` value, the currency service `INVALID_ARGUMENT`, checkout `INTERNAL`, and failed value in `ai.checkout.value_usd`. `fault.injected=true` is reserved for the deterministic fallback and must not be presented as an LLM decision.

## Customer accounts on Postgres

**Status: implemented and tested locally; not deployed.** Nothing below has been applied to the cluster.

> **Superseded in part:** the account experience was extended (accountservice, customer dashboard, durable
> idempotent checkout, saved demo payment methods, assistant order tools). See
> [account-experience-runbook.md](account-experience-runbook.md) for the architecture, configuration, rollout and
> rollback, and [account-experience-telemetry.md](account-experience-telemetry.md) for exactly what is and is not
> exported. With `ACCOUNT_BACKEND=local` the flow described below is still available.

### Design
- Postgres is the source of truth for customers, addresses, activation claims and login events (`src/shoppingassistantservice/migrations/0001_accounts.sql`). Passwords are PBKDF2 hashes.
- The assistant applies migrations itself at start-up on a dedicated connection inside one transaction, guarded by `pg_try_advisory_xact_lock`. It tracks readiness (`unknown|migrating|ready|stale|failed`) bound to a per-database `instance_id`, and revalidates on connection errors, new connections and every 60 seconds, so a restarted or replaced database is migrated again without restarting the pod. `/readyz/accounts` reports this; `/healthz` stays independent of Postgres.
- Chat account creation is server-side. The activation code is never in a browser cookie, a response, Bedrock, a log, a span or a metric. Postgres keeps only hashed claims bound to the browser session; the raw code lives in Redis (`activation_handoff:<sha256(session)>`, 900 s) and can be regenerated for the same session only. A different session cannot take over a pending account. Expired pending accounts are reaped.
- Sessions in Redis carry an identity snapshot (`email`, `customer_id`, `name`). When Postgres is down a logged-in customer keeps their identity, profile pages show an unavailable message instead of logging them out, and the assistant says saved details are unavailable instead of silently switching to guest. Guest checkout does not use Postgres.

### Telemetry
- psycopg client spans come from `opentelemetry-instrumentation-psycopg` (attributes verified from the installed version: `db.system`, `db.name`, `db.statement`, `db.user`, `net.peer.name`, `net.peer.port`). Statements are parameterized; no sqlcommenter; no parameter capture.
- Business spans `accounts.db.<operation>` wrap them and carry synthetic customer name, email and id. Metrics: `app.account.db.duration`, `app.account.pending_reaped`, `app.account.activation.events`, with no customer identifiers as labels.
- Never exported: shipping address, passwords and hashes, activation codes and their hashes, session tokens and hashes, cookies, payment credentials, internal credentials. Enforced by `tests/test_accounts_pg.py`.

### Local tests
```
cd src/shoppingassistantservice
python tests/test_accounts_pg.py      # real Postgres (bundled via pgserver) + fake Redis
python tests/test_checkout.py
python tests/test_agent_local.py
python tests/test_telemetry_redaction.py
cd ../frontend && go test ./...
```

### Rollout (every step needs explicit approval)
1. **Read-only precheck** (you run it): `deploy/accounts-db/PRECHECK.md` - storage classes, EBS CSI, Redis, NetworkPolicy enforcement, Observe agent scrape settings, image digests, and a `kubectl diff` of exactly what the branch would change.
2. Create secrets: `deploy/observe/create-account-secrets.sh` (also creates the missing `shopping-assistant-account-api`).
3. Apply `deploy/accounts-db/accounts-db.yaml` separately (it is NOT in the skaffold/kustomize overlay, so `skaffold run` never creates it), then build, push and deploy with skaffold. Deploy the database first and wait for `pg_isready`; the assistant migrates on start.
4. Optional metrics: `deploy/observe/values-accounts-db-metrics.yaml` (changes the agent configuration).
5. Verify in Observe: DB spans nested under account tool spans, no sensitive attributes, then the outage and replacement drills (scale `accounts-db` to 0 and back; delete the PVC only in a throwaway namespace).

### Rollback limits
- Reverting the assistant image to the Redis-only version does **not** bring back accounts created in Postgres; the old code cannot read them. Keep the database and PVC (use a Retain storage class) when rolling back.
- Role passwords in `accounts-db-credentials` are applied once, when the data directory is first initialised. Rotating the secret later requires `ALTER ROLE` inside the database.
- One replica, one volume: a node failure means an outage until the pod reschedules. There is no backup in this demo; add `pg_dump` to a bucket before keeping real data.

## Demo Sequence

1. Open the storefront's assistant page and ask `Find me a mug under $50.`
2. In Observe, open that trace. Confirm `invoke_agent`, `chat`, `execute_tool search_products`, and the ProductCatalogService spans share one trace.
3. On the `chat` span, show the model, the `gen_ai.usage.*` tokens, and `gen_ai.input.messages`/`gen_ai.output.messages` with the tool call.
4. Run a guided checkout conversation: add an item, say `I'd like to check out.`, provide an email or choose guest checkout, select a synthetic payment method, answer the currency question, and confirm the review.
5. Enable the prompt regression:

   ```bash
   ./deploy/observe/ai-demo-toggle.sh v2
   ```

   `v2` tells the model to pass currencies the way the customer said them. Whether Claude actually sends `"Japanese Yen"` is the model's own decision, and it **has not been verified live**.

6. If `v2` doesn't fail reliably, use the explicitly synthetic fallback:

   ```bash
   ./deploy/observe/ai-demo-toggle.sh inject-on
   ```

   This rewrites the model's ISO code after the model returns it. It labels the span with `fault.injected=true`, `fault.names`, and `app.tool.args_source=synthetic_fault_injection`, and keeps the model's original arguments. Don't present it as a model decision.

7. Investigate the failure:
   - Follow `execute_tool place_order` (ERROR) -> `CheckoutService/PlaceOrder` (INTERNAL) -> `CurrencyService/Convert` (`INVALID_ARGUMENT: unsupported currency code`).
   - Filter on `app.tool.currency_valid=false` and group by `prompt.version`.

8. Show the business impact:
   - `ai.checkout.attempts` by outcome and `prompt.version`.
   - `ai.checkout.value_usd` with outcome=failed. This is at-risk value, not confirmed lost revenue.
   - `ai.checkout.retries`.
   - `ai.checkout.recovered_value_usd`.

9. Restore:

   ```bash
   ./deploy/observe/ai-demo-toggle.sh inject-off
   ./deploy/observe/ai-demo-toggle.sh v1
   ```

   Then check out again and confirm it recovers. A later success in the same session records recovered value.

## Synthetic Traffic

The Locust `AssistantUser` writes its prompts from templates, so the only Bedrock spend is the assistant's own replies. Each conversation keeps one customer cookie and one conversation ID across turns. Failed checkouts trigger "why didn't it work?" and retry follow-ups.

With the default `USERS=10`, the weights produce one assistant user to nine storefront users. You can tune it with these settings:

- `ASSISTANT_USERS_WEIGHT` (set to 0 to disable)
- `ASSISTANT_SCENARIOS`
- `ASSISTANT_MAX_TURNS`
- `ASSISTANT_THINK_MIN`/`MAX`
- `ASSISTANT_GAP_MIN`/`MAX`
- `ASSISTANT_WHY_PROB`
- `ASSISTANT_RETRY_PROB`
- `ASSISTANT_IDENTITY_POOL` (default 25 synthetic identities)
- `ASSISTANT_RETURNING_PROB` (default 0.4)
- `ASSISTANT_GUEST_PROB` (default 0.15)
- `ASSISTANT_CHECKOUT_TURNS` (default 14 for guided checkout)

## Validation Status

**Verified locally** with `python tests/test_agent_local.py` in `src/shoppingassistantservice`, which reports `ALL PASSED`. The harness drives the real `boto3` Converse client and botocore instrumentation against a fake Bedrock server and fake gRPC services. It checks:

- Trace parenting and baggage propagation.
- Content on the chat span.
- The v2 bad argument labelled as the model's own decision.
- Fault-injection labelling.
- Honest failure reporting.
- Business metrics.
- The no-credentials case returning 503.

**Not yet verified:**

- Bedrock model access and the model ID in your account.
- Claude's real behaviour under `v2`.
- The IAM and Pod Identity binding.
- EKS deployment, and that Observe ingests and recognizes the GenAI attributes.
- The real Node currencyservice marking its server span ERROR. Source reading says it will.
