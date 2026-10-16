# AI Shopping Assistant Demo

## What This Demonstrates

The shopping assistant is a Python Flask agent that uses Snowflake Cortex through the OpenAI-compatible Chat Completions API. The default model is `claude-sonnet-4-5`; all model inference remains inside Snowflake.

The trace should follow:

`frontend /bot` -> `invoke_agent` -> Cortex `chat` -> `execute_tool` -> Online Boutique gRPC services

The assistant supports product search, product lookup, currency conversion, cart operations, and checkout.

## Configuration

Create the Cortex secret in the target namespace without committing the PAT:

```bash
CORTEX_ACCOUNT_URL=https://<account>.snowflakecomputing.com \
  ./deploy/observe/create-cortex-secret.sh
```

The PAT's default role needs Cortex REST API access, such as `SNOWFLAKE.CORTEX_REST_API_USER` or `SNOWFLAKE.CORTEX_USER`. Use a dedicated demo account and role, not a corporate credential.

The service uses these defaults:

- `CORTEX_MODEL=claude-sonnet-4-5`
- `PROMPT_VERSION=v1`
- `FAULT_INJECTION=`
- `MAX_TOOL_STEPS=5`
- `MAX_COMPLETION_TOKENS=1024`
- `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY`

`SPAN_ONLY` is intentional. The OTel OpenAI instrumentation does not treat `true` as a valid content-capture value; that setting falls back to `NO_CONTENT`.

## Demo Sequence

1. Deploy the Observe/Skaffold overlay after confirming the target EKS cluster, namespace, AWS account, and ECR repository.
2. Open the storefront and select the assistant, or send a request to `/bot` through the frontend.
3. Ask: `Find me a mug under $50.`
4. In Observe, inspect the trace for the frontend request. Confirm the `invoke_agent`, Cortex `chat`, `execute_tool search_products`, and Product Catalog spans share one trace.
5. Confirm the chat span contains `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.request.model`, and token usage.
6. Run a checkout conversation using a currency name, such as `Please check out in Japanese Yen.`
7. Enable the prompt-regression configuration:

```bash
./deploy/observe/ai-demo-toggle.sh v2
```

8. Generate checkout traffic. `v2` changes the currency tool description to encourage natural-language currency values. Whether Claude emits an invalid value is model-dependent and must be verified live.
9. If the real model does not reproduce the failure reliably, enable the explicitly synthetic fallback:

```bash
./deploy/observe/ai-demo-toggle.sh inject-on
```

The fallback rewrites the model's ISO code after the model response. It records `fault.injected=true`, `fault.names`, `app.tool.args_source=synthetic_fault_injection`, and preserves the original model arguments. Do not describe this as an independently generated model decision.

10. Investigate the failed checkout from the `execute_tool place_order` span through Checkout and Currency Service. The invalid currency is rejected as `INVALID_ARGUMENT`; the checkout returns a failure rather than claiming success.
11. Compare `ai.checkout.value_usd`, failed attempts, retries, and recovered value. Failed value is at-risk value, not confirmed lost revenue.
12. Restore healthy behavior:

```bash
./deploy/observe/ai-demo-toggle.sh inject-off
./deploy/observe/ai-demo-toggle.sh v1
```

13. Generate another checkout and confirm recovery. A later success in the same session records recovered value.

## Synthetic Traffic

The Locust assistant user sends templated prompts and does not use an LLM to generate traffic. This avoids extra Cortex calls. It preserves a fixed customer cookie and conversation ID across turns.

The default load split is nine storefront users to one assistant user. Relevant controls include:

- `ASSISTANT_USERS_WEIGHT`
- `ASSISTANT_SCENARIOS`, for example `browse=28,checkout=24,cart=12`
- `ASSISTANT_MAX_TURNS`
- `ASSISTANT_THINK_MIN` and `ASSISTANT_THINK_MAX`
- `ASSISTANT_GAP_MIN` and `ASSISTANT_GAP_MAX`
- `ASSISTANT_WHY_PROB` and `ASSISTANT_RETRY_PROB`

The assistant traffic includes browsing, comparisons, currency conversion, cart operations, checkout, support, off-topic prompts, and synthetic prompt-injection attempts.

## Local Validation

The local agent harness uses a fake Cortex HTTP server and fake gRPC services. It validates tool execution, trace parenting, baggage propagation, prompt capture, metrics, error handling, and synthetic fault labels. It does not validate live Cortex access or real EKS services.

```bash
cd src/shoppingassistantservice
python tests/test_agent_local.py
```

The resolved dependency set was tested with `pip check`. The frontend compiles with `go build ./...` and the checkout service passes `go vet ./...`.

## EKS and ECR Deployment

Deployment is intentionally not automatic. Before applying anything:

1. Confirm the AWS account, region, EKS cluster, namespace, and IAM permissions.
2. Create or select the ECR repository for `shoppingassistantservice` and verify repositories for any changed images.
3. Authenticate Docker to ECR.
4. Generate `deploy/observe-skaffold/service-version.env` using the current Git short SHA.
5. Build and push with Skaffold using the correct ECR default repository.
6. Render and inspect the manifests, especially image references and the Cortex Secret reference.
7. Request explicit approval before applying to EKS.

The local environment did not have a running Docker daemon, and cluster operations were not performed. Cortex REST tool-calling and Observe AI-specific ingestion remain pending live validation with a dedicated demo PAT and an approved EKS deployment.
