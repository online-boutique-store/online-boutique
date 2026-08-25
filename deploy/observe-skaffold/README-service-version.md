# service-version.env

This directory expects a file named `service-version.env` (git-ignored,
not committed) containing a single line:

    OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production,deployment.environment.name=production,service.version=<current-git-sha>

Generate it immediately before every `skaffold run` so `service.version`
tracks the commit actually being deployed:

    echo "OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production,deployment.environment.name=production,service.version=$(git rev-parse --short HEAD)" > deploy/observe-skaffold/service-version.env

Observe creates a deployment marker whenever it sees a span with a new
service.version value. Without this file (or if it goes stale), no marker
fires. See: https://docs.observeinc.com/docs/monitor-and-track-new-deployments-on-your-service
