# The authoring side — declarations, and the facts they write

**Role:** a sketch of what a delivery program *declares* so that the tracker
in `out/index.html` materialises from it. Not a runnable API and not a step
list: the existing prototypes (Joe's `Stack`/`Stage`/`Gate`/`Job`, Vic's
`delivery.Program` with a `stages: [update, check, approval, …]` array)
describe *runs*. This describes the **standing subjects** — the things facts
accumulate on — and the emit contract between a program, the operations
people perform, and the executors that do the work. Syntax is placeholder;
the mapping table is the point.

## One program for pulumi-service

```ts
// sketch — there is no such package
import * as d from "@pulumi/delivery";

export default d.program("pulumi-service", {

  // Where freight comes from. A build on master is discovered as freight:
  // program artifact (the five images) × static config, content-keyed.
  warehouse: d.warehouse.ociBuild({
    repo: "pulumi/pulumi-service", branch: "master",
    images: ["api", "console", "jobs", "ratelimit", "workflow"],
    config: d.esc("pulumi/service"),          // pinned version rides in the freight
  }),

  // Stages are registered, standing subjects. Each declares its upstream
  // consumer-side; there is no pipeline object — the DAG is a query.
  stages: {
    "testing":       d.stage({ region: "us-west-2",    upstream: "warehouse",
                               promote: d.auto(),
                               annotate: { owner: "internal-tools", slack: "#ops-notif-testing" } }),
    "testing-eu":    d.stage({ region: "eu-central-1", upstream: "warehouse",
                               promote: d.auto() }),
    "staging":       d.stage({ region: "us-west-2",    upstream: d.releaseTrain(),
                               promote: d.auto({ requires: [d.verified("testing", "integration-tests")] }),
                               annotate: { url: "https://app.pulumi-staging.io", slack: "#ops-notif-staging" } }),
    "production":    d.stage({ region: "us-west-2",    upstream: "staging",
                               promote: d.gated({
                                 requires: [d.verified("staging", "integration-tests"),
                                            d.verified("staging", "smoke"),
                                            d.verified("staging", "load-generator"),
                                            d.approved({ role: "oncall", via: d.releasePrMerge() })],
                               }),
                               annotate: { url: "https://app.pulumi.com", slack: "#ops-alerts" } }),
    "production-eu": d.stage({ region: "eu-central-1", upstream: "production",
                               promote: d.autoIfSafe({
                                 requires: [d.verified("production", "integration-tests")],
                                 safe:     d.plan(p => p.delete == 0 && p.replace == 0 && !p.migrationsChanged),
                                 else:     d.approved({ role: "oncall" }),
                               }) }),
  },

  // How each stage is enacted: an executor, not the orchestrator.
  enact: d.pulumiUp({ stack: s => `pulumi/pulumi-service/${s}`, strategy: "ecs-rolling" }),

  // Verifications write onto the (stage, freight) pair, not nodes into a workflow.
  verify: {
    "integration-tests": d.watch.githubWorkflow("integration-tests.yml"),
    "smoke":             d.watch.githubWorkflow("deploy-smoke-test.yml"),
    "load-generator":    d.watch.http("…/load-generator/status", ok: r => r.success == 1.0, window: "10m"),
  },

  // Wiring is declared, consumer-resident data. Uptake is a promotion on the edge.
  bindings: [
    d.bind({ consumer: "workflow-pool@production",    key: "ami_id",
             from: d.outputs("workflow-ami@production"),    uptake: "gated" }),
    d.bind({ consumer: "workflow-pool@production-eu", key: "ami_id",
             from: d.outputs("workflow-ami@production-eu"), uptake: "gated" }),
  ],
});
```

Nothing in this program is a step body. The order of stages is not the
order they're written in; it's the `upstream` declarations. There is no loop
for blue/green and no `if` for the migration check — the rollout strategy
belongs to the enactment, and the check is a predicate over a plan fact.

## What writes what

| who | when | writes (class · kind · subject) |
|---|---|---|
| **program apply** | `pulumi up` on the delivery program (a re-apply that changes nothing writes nothing) | intent · `stage.declared` · `stage:<s>` — with the annotations as payload (view configuration: URLs, owner, Slack) |
| | | intent · `policy.declared` · `stage:<s>` — mode + rule text |
| | | intent · `binding.declared` · `edge:<consumer><-<producer>.<key>` |
| **warehouse** | a master build completes | observation · `freight.discovered` · `freight:<F>` — digests, config version, source SHA, PR membership |
| **release cron / button** | 09:00 weekdays, or "Cut release" | intent · `release.cut` · `freight:<F>` — the train nominates a freight; `armed_by` names the person |
| **policy engine** | on any fact that could satisfy a stage's rule | intent · `promotion.decided` · `stage:<s>` — actor `policy:<s>`, `refs` = the facts that satisfied the rule |
| **a person** | merges the release PR / runs `delivery approve` | intent · `approval.granted` · `stage:<s>` — role, via, rationale |
| **a person** | `delivery hold production-eu --until …` | intent · `hold.placed` · `stage:<s>` — expiry in payload |
| **a person, side door** | does something out of band and says so | intent · `breakglass.recorded` · `stage:<s>` — scope, action, from/to, incident, expiry |
| **a person** | takes up a published record | intent · `uptake.decided` · `edge:<…>` — record version |
| **executor** | starts / progresses / finishes an enactment | observation · `transition.started` / `.phase` / `.finished` · `transition:<T>`; `resource.step` at whatever grain the executor emits |
| **verification watch** | an external check concludes | observation · `verification.recorded` · `stage:<s>` — freight, check, outcome |
| **planner** | a preview runs (on candidacy, or ahead of it) | observation · `plan.summarized` · `stage:<s>` — counts, migrations touched, baseline |
| **producer enactment** | a stack publishes outputs | observation · `output.published` · `record:<producer>` — versioned |
| **conformance watch** | on cadence | observation · `state.observed` · `stage:<s>` — what is actually running |
| **side jobs** | sentry marker, PR notifications | observation · `job.finished` · `stage:<s>` — with `optional` |

Two doors, one destination: the merge that approves and the ECS console
click that rolls back both land as intent facts with an actor and a reason.
That is what lets the grid explain drift instead of fighting it.

## What annotations are

View configuration, not data. `annotate:` puts the environment URL, the ops
console path, the owning team, and the Slack channel on the stage subject so
the grid can render links and route notifications. Nothing in the tracker's
*state* comes from an annotation; a stage with none still renders — it just
has nothing to link to.

## Where this composes with the other POCs

Tyler's orchestration POC and Florian's request-and-yield pump sit in the
`enact:` slot and the policy engine's slot: they decide *when* to run and
*run* things. Their emit contract is the observation rows above
(`transition.*`, `verification.recorded`, `plan.summarized`). If their runs
write those rows, this tracker renders them unchanged. The intent rows come
from people and policies, not from executors — that boundary is the
publication ≠ uptake discipline, and it's what keeps evidence from quietly
becoming normative.
