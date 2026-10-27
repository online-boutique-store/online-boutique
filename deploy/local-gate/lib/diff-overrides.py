#!/usr/bin/env python3
"""Fail if applied manifests differ from the allowed local-only override paths."""
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML is required for diff-overrides.py")

ALLOWED = {
    ("Deployment", "shoppingassistantservice"): {"spec.template.spec.containers.server.env.BEDROCK_ENDPOINT_URL",
        "spec.template.spec.containers.server.env.AWS_ACCESS_KEY_ID",
        "spec.template.spec.containers.server.env.AWS_SECRET_ACCESS_KEY",
        "spec.template.spec.containers.server.env.AWS_REGION",
        "spec.template.spec.containers.server.env.PROMPT_VERSION",
        "spec.template.spec.containers.server.env.RATE_LIMIT_RPM"},
    ("Deployment", "loadgenerator"): {"spec.replicas"},
}

def index(obj, path=""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out.update(index(v, f"{path}.{k}" if path else k))
        return out
    if isinstance(obj, list):
        # Keep env lists addressable by variable name and container lists by name.
        out = {}
        for item in obj:
            if isinstance(item, dict) and "name" in item:
                out.update(index(item, f"{path}.{item['name']}"))
            else:
                out.update(index(item, f"{path}[]"))
        return out
    return {path: obj}

def docs(path):
    with open(path) as f:
        return [d for d in yaml.safe_load_all(f) if d]

def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: diff-overrides.py rendered.yaml applied.yaml")
    rendered = {(d.get("kind"), d.get("metadata", {}).get("name")): d for d in docs(sys.argv[1])}
    applied = {(d.get("kind"), d.get("metadata", {}).get("name")): d for d in docs(sys.argv[2])}
    unexpected = []
    for key, before in rendered.items():
        after = applied.get(key)
        if after is None:
            unexpected.append((key, "object missing")); continue
        bi, ai = index(before), index(after)
        for path in sorted(set(bi) | set(ai)):
            # Server-populated fields (status, resourceVersion, defaults) are not
            # overrides: live-only paths matter only where behaviour can change.
            if path not in bi and not (".env." in path or path.endswith((".command", ".args"))
                                       or ".volumes" in path or ".volumeMounts" in path):
                continue
            if path.startswith(("status", "metadata.managedFields", "metadata.resourceVersion",
                                "metadata.uid", "metadata.generation", "metadata.creationTimestamp",
                                "metadata.annotations.deployment.kubernetes.io",
                                "metadata.annotations.kubectl.kubernetes.io")):
                continue
            if bi.get(path) != ai.get(path):
                # Image substitutions are Skaffold's normal local registry transformation.
                allowed = path.endswith(".image") or any(
                    path == a or path.startswith(a + ".") for a in ALLOWED.get(key, set()))
                if not allowed:
                    unexpected.append((key, path, bi.get(path), ai.get(path)))
    if unexpected:
        print(json.dumps(unexpected, indent=2, default=str))
        return 1
    print("FIELD_LEVEL_OVERRIDE_DIFF_PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
