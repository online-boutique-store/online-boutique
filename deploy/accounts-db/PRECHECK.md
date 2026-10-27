# Accounts database: read-only EKS precheck

Every command here only reads. None creates, changes or deletes anything. `kubectl diff` in
section 8 uses a server-side dry run and applies nothing. Run them yourself and share the
output (remove anything that looks like a secret value). The storage and scrape decisions are
made from these results, not assumed.

```bash
export NS=online-boutique          # app namespace
export CLUSTER=<eks-cluster-name>
export AWS_REGION=us-west-2
```

## 0. Confirm you are pointed at the right cluster
```bash
kubectl config current-context
aws sts get-caller-identity --query Account --output text
aws eks describe-cluster --name "$CLUSTER" --query 'cluster.{version:version,platform:platformVersion}'
```

## 1. StorageClasses, provisioners, reclaim policies
```bash
kubectl get storageclass -o custom-columns='NAME:.metadata.name,PROVISIONER:.provisioner,RECLAIM:.reclaimPolicy,BINDING:.volumeBindingMode,EXPAND:.allowVolumeExpansion,DEFAULT:.metadata.annotations.storageclass\.kubernetes\.io/is-default-class,TYPE:.parameters.type'
kubectl get storageclass -o yaml
```
What to look for:
- A class with provisioner `ebs.csi.aws.com`, `parameters.type: gp3`, `reclaimPolicy: Retain`,
  `volumeBindingMode: WaitForFirstConsumer` is the preferred choice.
- `kubernetes.io/aws-ebs` is the legacy in-tree provisioner (often the default `gp2`). On recent
  EKS versions it only works through CSI migration.
- If no gp3/Retain class exists, creating one is a separate change that needs approval.
  Another option is a Delete-policy class with the PV patched to Retain after binding.

## 2. EBS CSI driver
```bash
kubectl get csidriver
aws eks list-addons --cluster-name "$CLUSTER"
aws eks describe-addon --cluster-name "$CLUSTER" --addon-name aws-ebs-csi-driver \
  --query 'addon.{status:status,version:addonVersion,role:serviceAccountRoleArn}'
kubectl -n kube-system get deploy,daemonset -l app.kubernetes.io/name=aws-ebs-csi-driver -o wide
kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-ebs-csi-driver
```
If the add-on isn't installed, it may be self-managed. In that case look for `ebs-csi-controller`:
```bash
kubectl -n kube-system get pods | grep -i ebs
```

## 3. Node capacity, zones, architecture (EBS volumes are zonal)
```bash
kubectl get nodes -L topology.kubernetes.io/zone -L kubernetes.io/arch -L node.kubernetes.io/instance-type
kubectl describe nodes | grep -A8 'Allocated resources'
kubectl top nodes 2>/dev/null || echo "metrics-server not available"
```

## 4. Current Redis StatefulSet / Deployment / PVC
Redis matters here because the branch also changes it: the rendered manifests replace the
`redis-cart` Deployment with a StatefulSet. See section 8.
```bash
kubectl -n "$NS" get deploy,statefulset,pod,pvc -l app=redis-cart -o wide
kubectl -n "$NS" get statefulset redis-cart -o yaml 2>/dev/null | grep -nE 'image:|storageClassName|storage:|appendonly|replicas'
kubectl -n "$NS" get deploy redis-cart -o yaml 2>/dev/null | grep -nE 'image:|emptyDir|volumes|replicas'
kubectl get pv -o custom-columns='NAME:.metadata.name,CLAIM:.spec.claimRef.name,NS:.spec.claimRef.namespace,CLASS:.spec.storageClassName,RECLAIM:.spec.persistentVolumeReclaimPolicy,SIZE:.spec.capacity.storage,STATUS:.status.phase' | grep -E 'NAME|redis'
kubectl -n "$NS" get endpoints redis-cart -o wide     # >1 address = two Redis backends behind one Service
```

## 5. NetworkPolicy enforcement
```bash
kubectl get networkpolicy -A
kubectl -n kube-system get daemonset aws-node -o jsonpath='{.spec.template.spec.containers[*].name}{"\n"}{.spec.template.spec.containers[0].image}{"\n"}'
aws eks describe-addon --cluster-name "$CLUSTER" --addon-name vpc-cni \
  --query 'addon.{version:addonVersion,config:configurationValues}'
kubectl -n kube-system get daemonset aws-node -o yaml | grep -n -i -A1 'enable-network-policy\|ENABLE_NETWORK_POLICY'
kubectl get pods -A | grep -Ei 'calico|cilium|tigera|network-policy' || echo "no third-party policy engine pods"
```
NetworkPolicy is enforced only if `aws-node` runs an `aws-network-policy-agent` container with
network policy enabled, or Calico/Cilium is installed. Otherwise the policy in
`accounts-db.yaml` has no effect, and any pod in the cluster can reach the database port.
Also check whether the repository's optional `network-policies` skaffold profile (default-deny)
is in use. Apply it only together with an egress rule for assistant -> Postgres.

## 6. Observe agent Helm chart and Prometheus scraping
`helm get values` can print credentials. Before sharing it, check for and remove any token value.
```bash
helm list -A | grep -i observe
helm -n observe        get values <release>     # personal agent
helm -n observe-demo   get values <release>     # demo agent (values-demo-agent.yaml)
helm list -A -o json | python3 -c 'import json,sys;[print(r["name"],r["namespace"],r["chart"],r["app_version"]) for r in json.load(sys.stdin) if "observe" in r["chart"]]'
helm repo list
helm show values <repo>/<chart> --version <chart-version> | grep -n -i -B2 -A40 'prometheusScrape'
helm show values <repo>/<chart> --version <chart-version> | grep -n -i -A20 'prometheus-scraper'
kubectl -n observe-demo get configmap | grep -i -E 'prom|scrape'
kubectl -n observe-demo get configmap <scraper-configmap> -o yaml | grep -n -i -A25 'kubernetes_sd\|scrape_configs\|annotation'
```
What to look for:
- The supported keys for annotation discovery: enable flag, namespace include/exclude filters,
  port/path annotation names.
- Whether the scraper supports a static job or extra `scrape_configs`.
- Which namespaces the scraper may reach. Section 5's policy allows only `observe` and
  `observe-demo` to reach port 9187.

## 7. Image digests (run where docker/crane/skopeo is available)
```bash
# latest 16.x patch tags
crane ls docker.io/library/postgres | grep -E '^16\.[0-9]+$' | sort -V | tail -3
# multi-arch index digest (pin this one so both amd64 and arm64 nodes work)
crane digest docker.io/library/postgres:16.4
crane digest quay.io/prometheuscommunity/postgres-exporter:v0.15.0
# alternatives
docker buildx imagetools inspect docker.io/library/postgres:16.4 | head -5
docker buildx imagetools inspect quay.io/prometheuscommunity/postgres-exporter:v0.15.0 | head -5
skopeo inspect --format '{{.Digest}}' docker://docker.io/library/postgres:16.4
```
Pin as `image: postgres:16.4@sha256:<digest>`. Use a newer 16.x patch if one is available.
The node architecture from section 3 must be included in the index.

## 8. What the branch would change (server-side dry run, nothing applied)
This is the check for "will deploying this branch disturb the running app". It renders the same
manifests `skaffold run` would deploy and asks the API server for the diff.
```bash
skaffold render --offline=true --digest-source=none \
  --default-repo=<ECR registry>/online-boutique -n "$NS" > /tmp/ob-render.yaml
kubectl diff -n "$NS" -f /tmp/ob-render.yaml | grep -E '^(\+|-)' | grep -vE '^(\+\+\+|---)|image:|generation' | head -200
# secrets the rendered pods require (key names only; values never printed)
for s in shopping-assistant-account-api accounts-db-credentials; do
  kubectl -n "$NS" get secret "$s" -o go-template='{{.metadata.name}}: {{range $k,$v := .data}}{{$k}} {{end}}{{"\n"}}' 2>/dev/null || echo "$s: MISSING"
done
```
The database manifest `deploy/accounts-db/accounts-db.yaml` is intentionally **not** part of
that render. Check it separately:
```bash
kubectl diff -n "$NS" -f deploy/accounts-db/accounts-db.yaml   # dry run; everything shows as new
```
