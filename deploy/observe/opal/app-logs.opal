// App_Logs — container logs enriched with user + customer identity
// PRIMARY input: OpenTelemetry Logs (Kubernetes Explorer)
// Secondary:     User_Sessions    (role: Data)
//                Customer_Records (role: Data)
//                Commit_Context   (role: Data)

// body is already a String; parse it once and reuse.
make_col body_json:parse_json(body)

make_col
    message:string(body_json.message),
    severity:string(body_json.severity),
    http_req_id:string(body_json["http.req.id"]),
    http_method:string(body_json["http.req.method"]),
    http_path:string(body_json["http.req.path"])

// The identity key. Two sources, same value:
//   attributes.UserId  — .NET cartservice structured-log property. The
//                        body only ever contains the "{UserId}"
//                        template placeholder, never the value.
//   body_json.session  — logrus field on the Go frontend.
// Both carry the shop_session-id cookie, which loadgenerator pins to a
// roster UUID, so this equals app.customer.id from the span data.
make_col session_id:coalesce(
    string(attributes["UserId"]),
    string(body_json.session))

make_col
    deployment_name: string(resource_attributes["k8s.deployment.name"]),
    pod_name:        string(resource_attributes["k8s.pod.name"]),
    namespace_name:  string(resource_attributes["k8s.namespace.name"]),
    cluster_uid:     string(resource_attributes["k8s.cluster.uid"]),
    cluster_name:    string(resource_attributes["k8s.cluster.name"]),
    service:         string(resource_attributes["service.name"]),
    service_version: string(resource_attributes["service.version"]),
    trace_id:        string(fields["trace_id"]),
    span_id:         string(fields["span_id"]),
    github_repo:     "online-boutique-store/online-boutique"

// service.version arrives as "<tag>@sha256:<image digest>". That digest is
// the container image's, not git's — left() cuts at 8 to keep only the tag,
// which is the git short SHA from `git rev-parse --short HEAD`. Infra
// containers (redis "alpine", kube-proxy "v1.32.13-eksbuild.20") truncate
// to non-SHAs and will simply miss the commit lookup, which is correct.
make_col commit_sha:left(service_version, 8)

// -- path 1: resolve identity through the trace --------------------
// Covers log lines that carry a trace_id but log no session of their
// own — most of the backend gRPC services.
lookup on(trace_id=@User_Sessions.trace_id),
    session_via_trace:@User_Sessions.session_id,
    customer_via_trace:@User_Sessions.customer_name,
    tier_via_trace:@User_Sessions.customer_tier,
    ltv_via_trace:@User_Sessions.customer_ltv,
    country_via_trace:@User_Sessions.country

make_col session_id:coalesce(session_id, session_via_trace)

// -- path 2: resolve identity straight from the logged session -----
// Covers lines with a session but no usable trace_id, which the trace
// lookup alone would leave anonymous.
lookup on(session_id=@Customer_Records.uuid),
    customer_direct:@Customer_Records.name,
    tier_direct:@Customer_Records.customer_tier,
    ltv_direct:@Customer_Records.lifetime_value_usd,
    country_direct:@Customer_Records.country,
    email:@Customer_Records.email

// Direct match wins — it's derived from the log line itself rather
// than inferred across a join.
make_col
    customer_id:session_id,
    customer_name:coalesce(customer_direct, customer_via_trace),
    customer_tier:coalesce(tier_direct, tier_via_trace),
    customer_ltv:coalesce(ltv_direct, ltv_via_trace),
    country:coalesce(country_direct, country_via_trace)

// -- path 3: resolve deploy provenance through the commit SHA -----
// commit_sha is left(service_version, 8) from the container image tag.
// Commit_Context is a Table (non-temporal via statsby), so this lookup
// works for any log line regardless of when the commit landed — no 24h
// validity window. CI/PR columns resolve to null for commits with no
// workflow runs or no merge PR (currently true of the deployed afbc9785,
// which has no Actions Run entry and was pushed directly, not via PR).
lookup on(commit_sha=@Commit_Context.short_sha),
    commit_message:@Commit_Context.commit_message,
    commit_author:@Commit_Context.commit_author,
    commit_url:@Commit_Context.commit_url,
    commit_ref:@Commit_Context.commit_ref,
    commit_time:@Commit_Context.commit_time,
    ci_run_count:@Commit_Context.ci_run_count,
    ci_failure_count:@Commit_Context.ci_failure_count,
    ci_success_count:@Commit_Context.ci_success_count,
    ci_workflows:@Commit_Context.ci_workflows,
    ci_last_run_time:@Commit_Context.ci_last_run_time,
    ci_total_duration:@Commit_Context.ci_total_duration,
    ci_author:@Commit_Context.ci_author,
    deploy_conclusion:@Commit_Context.deploy_conclusion,
    has_ci_data:@Commit_Context.has_ci_data,
    pr_number:@Commit_Context.pr_number,
    pr_title:@Commit_Context.pr_title,
    pr_author:@Commit_Context.pr_author,
    pr_reviewers:@Commit_Context.pr_reviewers,
    pr_base_ref:@Commit_Context.pr_base_ref,
    pr_head_ref:@Commit_Context.pr_head_ref,
    pr_merge_date:@Commit_Context.pr_merge_date,
    pr_link:@Commit_Context.pr_link

set_col_visible timestamp:true
interface "log", log:body

pick_col
  timestamp,
  body,
  message,
  severity,
  session_id,
  customer_id,
  customer_name,
  email,
  customer_tier,
  customer_ltv,
  country,
  trace_id,
  span_id,
  service,
  service_version,
  commit_sha,
  commit_message,
  commit_author,
  commit_url,
  commit_ref,
  commit_time,
  ci_run_count,
  ci_failure_count,
  ci_success_count,
  ci_workflows,
  ci_last_run_time,
  ci_total_duration,
  ci_author,
  deploy_conclusion,
  has_ci_data,
  pr_number,
  pr_title,
  pr_author,
  pr_reviewers,
  pr_base_ref,
  pr_head_ref,
  pr_merge_date,
  pr_link,
  http_method,
  http_path,
  http_req_id,
  deployment_name,
  pod_name,
  namespace_name,
  cluster_name,
  cluster_uid,
  github_repo
