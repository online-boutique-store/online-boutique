# Observe by Snowflake — SE Demo Flow
**Format:** Live product demo only. No slides.
**Duration:** 35-40 min demo + 10 min Q&A

## Before You Start
- Set all views and the dashboard to the time window capturing the incident arc. Break, error spike, and recovery all visible, minimal flat baseline on either side. A 2-3 hour window works well
- Pre-locate the service map, checkoutservice Deployments tab, Errors tab, and a failing trace so you are not hunting mid-demo
- Have the Alerts view ready to open quickly
- Pull up the pre-demo email so you can quickly run through the scenario and roles at the top of the call

---

## 1. Set the Scene — No Screen (5 min)

No demo environment open yet. This is verbal only. Pull up the scenario email and run through it quickly to set the stage.

Before we dive into everything let's set the stage a bit.

You are all part of the engineering and platform team at an e-commerce company called Online Boutique. Here is a little background on your company and current situation:

- You run a microservices-based e-commerce application deployed on Kubernetes. Your storefront handles product browsing, cart management, checkout, payments, and order fulfillment across about 10 services
- Your engineering team ships multiple times a day. Velocity is high and that is a good thing, but it also means deployments are frequent and things occasionally break in production
- Right now your observability setup is fragmented. You have a dedicated logging tool, a separate tracing tool, a metrics platform, and a Kubernetes dashboard. Each one is a different interface, a different query language, and a different billing relationship
- When an incident happens, your engineers get paged and immediately start bouncing between those tools trying to manually piece together what happened. Your MTTD is actually pretty good because alerts fire fast. Your MTTR is the problem. Figuring out root cause takes too long because the context your engineers need is spread across four different places and they have to assemble it by hand
- On top of that your observability spend is climbing as your data volumes grow, and you are getting pressure from leadership to consolidate tooling without losing coverage
- Today you are evaluating Observe to see if it can replace your current stack with a single unified platform

Roles:

---

## 2. Demo Intro — Still No Screen (2 min)

Alright, thanks everyone for joining today. My name is Patrick Walsh and I will be taking you through the Observe platform today. Before we dive in I want to take a step back and align on what we heard from you in our previous conversations and what I plan to cover today.

- So in our last conversation you shared that your current toolset is fragmented. You currently own and manage multiple tools for logging, tracing, and monitoring your Kubernetes infrastructure. This is leading to delays in Mean Time to Remediate when incidents occur and bloated costs as your data volumes grow. Is that an accurate representation of your current state? Anything to add?
- So for the demo my plan is to first show how Observe gives you a single unified view of your logs, metrics, and traces so your team is not jumping between tools. Second, how that unified data model makes incident investigation dramatically faster. And third, we will walk through a real incident end-to-end, from the alert all the way to root cause, so you can see exactly what that investigation looks like inside the platform. Does that sound like it covers everything? Anything you want to make sure I hit today?

---

## 3. Unified View Dashboard (8 min)

Open the dashboard

Before walking the data, give them context on what makes Observe different:

- Before I walk you through what is on this dashboard, I want to give you a quick bit of context on why Observe is architected differently from other observability vendors, because it explains everything you are about to see
- Most observability vendors store your logs, metrics, and traces in separate specialized databases. A time-series DB for metrics, object storage for logs, something else for traces. Each system has its own query language, its own retention policy, its own cost model. Correlation between them is manual, which is exactly what breaks down during an incident when you need answers fast
- Observe was built from the ground up on Snowflake as a single unified data lake. Logs, metrics, and traces all land in the same place. There are no data silos. Correlation is not a feature they bolted on, it is a structural property of the backend. And because it is Snowflake under the hood, you are never looking at a sampled subset of your data. Every span, every log line, every metric is stored and queryable
- So what that means in practice is what you are looking at right now

Walk the dashboard:

- This dashboard shows an example of how we unify your logs, metrics, and traces in one single pane of glass. I built this myself and it took about 20 minutes, which gives you a sense of how quickly you can stand something like this up
- Top left you have got total errors across services. At a glance you can see the health of the system and any spikes in error volume
- Next to that is request duration by service, grouped by service, showing you where your slowest requests are coming from
- Bottom left is request rate, and next to that is throughput by service. This gives your team the traffic picture alongside the error picture
- Top right is a live log feed showing the latest log entries coming in across your services in real time
- And at the bottom we have a Trace Explorer with individual traces, error status, duration, service name, all filterable and drillable

Discovery question:

- Does a view like this exist for your team today? Or is this the kind of thing your engineers are assembling manually across multiple tools when they need the full picture?

---

## 4. Troubleshooting Scenario (20 min)

Set up the scenario before navigating anywhere:

- So now that you have a good sense of how Observe brings all your telemetry data together, let's see it in action and actually investigate an incident that occurred in our application
- A release went out to cartservice, checkoutservice, and frontend. Completely routine deploy, something your team does every day. But something in that release introduced a misconfiguration and errors started appearing shortly after. Your team got paged. Let's walk through how that investigation goes inside Observe

### Start in Alerts

Navigate to Monitoring → Alerts

- The first place most engineers land after getting paged is an alert, so let me show you what that looks like in Observe
- You can see the alert fired here with the relevant context. Which service, what threshold was breached, when it started
- From here I could actually drill directly into the traces associated with this alert and start troubleshooting immediately. Observe links the alert to the underlying telemetry automatically
- But for the purposes of this demo I am going to zoom out a bit and start in APM so you can see the full picture of what the service map looked like during this incident

### Service Map

Navigate to APM → Services → Map, switch to Errors view

- So this is the service map, a real-time topology of all your services and the traffic flowing between them. Right now we are looking at the window where that incident was happening
- You can immediately see frontend and checkoutservice lit up with errors. The service topology tells me the blast radius before I have written a single query. I know which services are affected and which ones are not
- Notice that cartservice and Redis are still present in the map. The dependency edge is intact, which tells me the service is not fully down. Something in the communication between them is broken

### Deployment Correlation

Navigate to APM → checkoutservice → Overview

- I am going to drill into checkoutservice since that is where errors are highest. The first question I always ask is did anything change recently?
- Point to the deployment marker on the chart
- There it is. A deployment marker, stamped automatically from the service.version attribute on our OTel spans. The moment a new version started sending traces, Observe created this marker. No manual webhook, no annotation required
- And you can see the error rate was flat, then a deployment happened, then errors spiked. That is the single most important correlation in incident response. What changed right before things broke. And the answer is right here without having to check a CI/CD system or dig through Slack

Navigate to the Deployments tab

- If I want to I can go to the Deployments tab and view traces specifically from this deployment version so I can isolate whether a particular release was the source of my problems
- Discovery: Today, how do your engineers connect a deployment to an error spike? Is that correlation automatic or is someone manually cross-referencing timestamps?

### Errors and Traces

Navigate to the Errors tab

- For this investigation I am going to go into the Errors tab. This is where Observe itemizes the errors occurring within this service so I can see exactly what is failing and how frequently
- You can see the errors bucketed here. Same error message, same failure point, repeating on every request since that deployment went out

Click into the top error, then click into a trace

- So let's drill into one of these. What I am about to show you is a distributed trace, and before I click in I want to give you a quick definition because this is worth understanding
- A trace represents a single user request as it flows through your entire system. Each colored bar is a span, which is a discrete unit of work within one service. The trace shows you the full journey: which service handled each step, how long it took, whether it succeeded or failed, and what the context was at that moment
- The reason traces are so valuable is that in a microservices architecture a single user action might touch 8 or 10 services. Without a trace you have no way to follow that request across service boundaries. You are just looking at individual logs with no connective tissue between them
- Show the trace waterfall
- You can see the request came into frontend, went to checkoutservice, checkoutservice called cartservice to retrieve the user's cart, and that is where it failed
- Click into the failing cartservice span
- Here is the Redis span. REDIS GET, failed. Connection refused on port 9999. And look at the span attributes: REDIS_ADDR is set to redis-cart:9999. The correct port is 6379. Someone misconfigured the Redis address in the cartservice deployment and it is pointing to the wrong port on the Redis instance

### Log Correlation

Navigate to the Logs related to trace tab within the trace view

- Now here is something I want to specifically call out because this is where a lot of tools fall short
- Most observability platforms treat logs and traces as completely separate systems. You find a trace, you copy the trace ID, you open your log tool in another tab, you paste it in and search. Every one of those steps is friction, and during an incident friction costs you time
- In Observe, logs and traces are correlated automatically via trace_id. These are the application logs emitted during this exact request, same trace context, surfaced right here inline. I have not left this view
- Point to the Redis error log line
- The log confirms it: StackExchange.Redis, it was not possible to connect to the redis server(s), ConnectTimeout. Same error, same timestamp, linked to the trace automatically
- This correlation works because Observe stores everything in the same Snowflake backend. There is no API call between a separate log system and a separate trace system. It is a join. That is why it is instant and why it works at full data fidelity with no sampling

### Resolution

- So at this point we have everything we need. The root cause is clear: the cartservice deployment went out with the wrong Redis connection address. It is pointing to port 9999 instead of 6379. The service is trying to connect to the right host but the wrong port, so every cart operation is failing
- The fix is straightforward. We take this back to the DevOps team, they update the REDIS_ADDR environment variable in the cartservice deployment to point to the correct Redis instance at port 6379, and push a corrected version
- Point to where the error rate drops and the second deployment marker appears
- There is the fix. Second deployment marker, error rate back to zero, service map goes green
- Complete timeline in one place: healthy state, deployment went out, errors appeared, root cause identified in the trace, fix deployed, recovery confirmed. All of that without opening a second tool

---

## 5. Value Summary (5 min)

Navigate back to the dashboard

- Let me bring this back to what matters for your team

- MTTD is table stakes, MTTR is the differentiator. Most tools alert well. The problem is what happens after the alert. We just went from alert to root cause in under five minutes, not because of a special runbook, but because logs, traces, and metrics are linked by default and your engineers are not context-switching between tools to assemble the picture

- The Snowflake foundation changes the economics. No sampling, no tiered retention, no "we only keep 7 days of traces." You pay for what you query, not for ingest spikes. Telemetry data doubles every two years and Observe's cost model scales with your actual usage, not your data volume

- Time to value is real. The Kubernetes integration is a Helm chart install. Everything you saw today, APM, log correlation, dashboards, was configured in a matter of hours, not a months-long professional services engagement

- Closing discovery: Based on what you have seen, where does your biggest gap sit today? Is it detection speed, investigation time, or the fragmentation between tools?

---

## 6. Q&A Preparation (10 min)

**Likely technical questions:**

- How does Observe handle sampling? No sampling required. Snowflake storage means you keep everything. Adaptive sampling is available if cost is a constraint.
- What is the data retention model? Configurable. Snowflake-backed so cost scales with actual query usage, not ingest rate spikes.
- How does this compare to Datadog / Honeycomb / Grafana? Observe's differentiator is the Snowflake-native data model. Everything is queryable via OPAL, datasets can be joined across signals, and there is no per-seat pricing forcing sampling tradeoffs. Datadog is powerful but expensive at scale. Honeycomb is trace-first and does not unify well with logs and metrics. Grafana requires significant self-assembly.
- What is OPAL vs. PromQL / LogQL / SPL? Single language across all signal types. Engineers learn one query pattern instead of three. OPAL compiles to optimized Snowflake SQL under the hood so you get the power of the data warehouse without needing to be a SQL expert.
- What about alerting? Observe's monitoring layer is built on the same dataset model. Alerts are OPAL queries with a threshold, no separate alerting config to maintain.
- Can we bring our own Snowflake account? Yes. Observe offers a Connected App deployment option where workloads run inside your existing Snowflake environment.

**Likely business questions:**

- What does pricing look like? Credit-based model tied to data ingest and query compute. No per-seat, no per-host pricing. This changes the economics at scale.
- How long does onboarding take? Kubernetes integration is a Helm chart install, first data flowing in under an hour. Full production rollout typically weeks, not months.
- What about compliance / data residency? Snowflake's data governance model applies. Data stays in your chosen region and cloud with a full audit trail.

---

## Demo Cheat Sheet

| Step | Action | Time |
|------|--------|------|
| Set scene | No screen, assign roles, run through scenario | 0:00 |
| Demo intro | Recap pain points, confirm scope, preview flow | 5:00 |
| Dashboard | Open dashboard, Observe architecture context | 7:00 |
| Walk dashboard | Errors, latency, throughput, logs, traces | 10:00 |
| Discovery | Is this view valuable? How do you do this today? | 14:00 |
| Scenario setup | Describe the incident before navigating | 15:00 |
| Alerts | Show alert, mention drill-in capability, zoom out | 16:00 |
| Service map | APM → Map → errors view, blast radius | 18:00 |
| Deploy marker | checkoutservice → error spike correlation | 21:00 |
| Deployments tab | Show version-scoped traces, discovery question | 23:00 |
| Errors tab | Itemized errors per service | 24:00 |
| Trace waterfall | Drill into error, explain trace/span, find Redis | 26:00 |
| Log correlation | Logs tab on trace, confirm root cause | 31:00 |
| Resolution | Explain fix, show recovery markers | 33:00 |
| Value summary | Back to dashboard, three key points, closing discovery | 35:00 |
| Q&A | | 40:00 |
