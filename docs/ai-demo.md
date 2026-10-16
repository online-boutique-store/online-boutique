# AI Shopping Assistant Demo

## What This Demonstrates

The shopping assistant is a Python Flask agent that calls Claude on **Amazon Bedrock** through the Converse API. It runs real tool calls against the existing Online Boutique gRPC services. The default model is the US cross-region inference profile `us.anthropic.claude-sonnet-4-6`. It is listed ACTIVE in account `739672403694`; the older `claude-sonnet-4-5` foundation model there is LEGACY.

A trace runs:

`frontend /bot` -> `invoke_agent shopping_assistant` -> `chat <model>` -> `execute_tool <name>` -> Online Boutique gRPC services

The tools are `search_products`, `get_product`, `convert_price`, `add_to_cart`, `get_cart`, and `place_order`.

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

- **On the `chat` span** as OTel semconv `gen_ai.input.messages` and `gen_ai.output.messages`, including tool-call names and arguments. The service adds these through botocore hooks, because the official instrumentation does not put content on spans.
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

## Demo Sequence

1. Open the storefront's assistant page and ask `Find me a mug under $50.`
2. In Observe, open that trace. Confirm `invoke_agent`, `chat`, `execute_tool search_products`, and the ProductCatalogService spans share one trace.
3. On the `chat` span, show the model, the `gen_ai.usage.*` tokens, and `gen_ai.input.messages`/`gen_ai.output.messages` with the tool call.
4. Run a checkout conversation: `Add two to my cart`, then `Please check out in Japanese Yen.`
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
