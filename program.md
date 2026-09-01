# The authoring side — declarations, and the facts they write

**Role:** a sketch of what a delivery program *declares* so that the tracker
in `out/index.html` materialises from it. Not a runnable API and not a step
list: the existing prototypes (Joe's `Stack`/`Stage`/`Gate`/`Job`, Vic's
`delivery.Program` with a `stages: [update, check, approval, …]` array)
describe *runs*. This describes the **standing subjects** — the things facts
accumulate on — and the emit contract between a program, the operations
people perform, and the executors that do the work. Syntax is placeholder;
the mapping table is the point, and `fixture.py` is its executable twin.

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
                               promote: d.auto(),      // the PR opening is the trigger; no precondition today
                               annotate: { url: "https://app.pulumi-staging.io", slack: "#ops-notif-staging" } }),
    "production":    d.stage({ region: "us-west-2",    upstream: "staging",
                               promote: d.gated({
                                 requires: [d.verified("staging", "integration-tests"),
                                            d.verified("staging", "smoke"),
                                            d.verified("staging", "load-generator"),
                                            d.approved({ role: "oncall", via: d.releasePrMerge() })],
                               }),
                               // A promotion to a freight older than the one the stage is decided to
                               // is gated here instead: cmd/prod-rollback's checks, as terms. No
                               // block → the ordinary terms apply in both directions.
                               rollback: d.autoIfSafe({
                                 requires: [d.carriedBefore({ within: "120h" }), d.notHeld()],
                                 safe:     d.plan(p => !p.migrationsChanged),
                                 else:     d.approved({ role: "oncall", via: "prod-rollback confirm" }),
                               }),
                               annotate: { url: "https://app.pulumi.com", slack: "#ops-alerts" } }),
    "production-eu": d.stage({ region: "eu-central-1", upstream: "production",   // proposed: today EU runs in parallel
                               promote: d.autoIfSafe({
                                 requires: [d.carried("production"),
                                            d.verified("production", "integration-tests"),
                                            d.notHeld()],
                                 safe:     d.plan(p => p.delete == 0 && p.replace == 0 && !p.migrationsChanged),
                                 else:     d.approved({ role: "oncall" }),
                               }) }),
  },

  // How each stage is enacted: an executor, not the orchestrator.
  enact: d.pulumiUp({ stack: s => `pulumi/pulumi-service/${s}`, strategy: "ecs-rolling" }),

  // Verifications write onto the (stage, freight) pair, not nodes into a workflow.
  verify: {
    "integration-tests": d.watch.githubWorkflow("integration-tests"),
    "smoke":             d.watch.githubWorkflow("deploy-smoke-test.yml"),
    "load-generator":    d.watch.http("…/load-generator/status", ok: r => r.success == 1.0, window: "10m"),
  },

  // Wiring is declared, consumer-resident data. Uptake is a promotion on the edge.
  bindings: [
    d.bind({ consumer: "workflow-pool@production",    key: "ami_id",
             from: d.outputs("workflow-ami@production"),    uptake: "gated" }),
    d.bind({ consumer: "workflow-pool@production-eu", key: "ami_id",
             from: d.outputs("workflow-ami@production-eu"), uptake: "gated" }),
    d.bind({ consumer: "workflow-pool@production",    key: "deploy_image_reference",
             from: d.outputs("service@production"),         uptake: "auto" }),
  ],
});
```

Nothing in this program is a step body. The order of stages is not the
order they're written in; it's the `upstream` declarations. There is no loop
for blue/green and no `if` for the migration check — the rollout strategy
belongs to the enactment, and the check is a term over a plan fact.

`requires:` is emitted as **structured terms**, not text: the policy fact
carries `terms: [{type: "verified", stage, check}, {type: "approved",
role}, …]` and the ledger's gate view evaluates them by joining the facts
each term names. The six term types in use — `verified`, `carried`,
`approved`, `not_held`, `plan_safe_or_approved`, `previously_carried` —
are the vocabulary a gate language needs first.

`rollback:` is the same shape under a second key. Which set a (stage,
freight) pair is evaluated against is not declared anywhere: the freight is
older than the one the stage is currently decided to, or it is not
(`v_direction` — relative to intent, so that an abort back to the stable
while a canary is in flight is a rollback too). Nothing
about a decision says "rollback"; the audit derives the direction the
decision had at the instant it was written from the same two facts.

## Two programs, no Uber program

The multistack scenario is two of these, owned by two teams, with the edges
between them declared on the consumer. Sketch of the payments side; the
platform side is the same shape with two stacks per stage and no cross-team
inputs.

```ts
export default d.program("payments", {
  warehouse: d.warehouse.ociBuild({ repo: "acme/payments", branch: "main",
    images: ["payments-api"], stacks: ["payments"],
    config: d.esc("acme/payments") }),           // base_image_version is pinned here

  stages: {
    dev:     d.stage({ environment: "dev",     upstream: "warehouse", promote: d.auto() }),
    staging: d.stage({ environment: "staging", upstream: "dev",
                       promote: d.auto({ requires: [d.verified("dev", "integration")] }) }),
    prod:    d.stage({ environment: "prod",    upstream: "staging",
                       promote: d.gated({ requires: [d.verified("staging", "integration"),
                                                     d.verified("staging", "canary"),
                                                     d.approved({ role: "payments-oncall" })] }),
                       // Returning prod to something it ran before needs no one; a failed
                       // canary analysis writes this decision by itself.
                       rollback: d.auto({ requires: [d.carriedBefore()] }),
                       // How prod is enacted: a canary with a pause the executor may only
                       // leave once these terms hold — facts recorded since the rollout began.
                       strategy: d.canary({ steps: [d.setWeight(10),
                                                    d.pause({ until: [d.verified("prod", "canary-analysis"),
                                                                      d.approved({ role: "payments-oncall", via: "promote" })] }),
                                                    d.setWeight(100)] }) }),
  },

  // Bindings are patterns: declared once, instantiated per environment.
  bindings: [
    // by reference: a per-stage record, taken up under this edge's own policy
    d.bind({ key: "cluster_endpoint", from: d.outputs("platform", "cluster"),   // platform's cluster stack, same environment
             uptake: { dev: d.auto(),
                       staging: d.autoIfSafe({ safe: d.preview(p => p.delete == 0 && p.replace == 0),
                                               else: d.approved({ role: "payments-oncall" }) }),
                       prod: d.gated({ requires: [d.notHeld(), d.approved({ role: "payments-oncall" })] }) } }),
    // by version: a pin in our config; the bump PR is the uptake and it rides our train
    d.bind({ key: "base_image_version", from: d.record("platform-images"), kind: "by-version",
             uptake: d.autoWithChecks() }),
  ],
});
```

The per-environment expansion is done by the program apply, not by SQL:
each instance edge cites its pattern (`pattern:` in the payload), and the
views read instances only. The uptake policy on a by-reference edge is the
same term vocabulary as a stage gate; on a by-version edge there is nothing
to gate at the edge — the consumer's stages gate the freight that carries
the pin.

A pin-set is written by whoever proposes it, not by either program:

```
pulumi delivery pin k8s-1.31 --member platform=P12 --member payments=A231 --order platform,payments \
  --because "1.31 rotates the OIDC issuer; A231 carries the issuer-aware auth lib"
```

## What writes what

| who | when | writes (class · kind · subject) |
|---|---|---|
| **program apply** | `pulumi up` on the delivery program (a re-apply that changes nothing writes nothing) | intent · `warehouse.declared` · `warehouse:<name>` |
| | | intent · `stage.declared` · `stage:<s>` — with the annotations as payload (view configuration: URLs, owner, Slack) |
| | | intent · `policy.declared` · `stage:<s>` — mode, trigger, structured `terms`, and the rule rendered from them |
| | | intent · `binding.declared` · `edge:<consumer><-<producer>.<key>` — the pattern (`role: pattern`), and one instance per environment citing it, with kind (by-reference \| by-version), uptake mode and structured `terms` |
| **warehouse** | a master build completes | observation · `freight.discovered` · `freight:<F>` — digests, config version, source SHA, the PRs this build introduced |
| **release cron / button** | 15:00 UTC weekdays, or "Cut release" | intent · `release.cut` · `freight:<F>` — the train nominates a freight |
| **policy engine** | on any fact that could satisfy a stage's terms, or its trigger | intent · `promotion.decided` · `stage:<s>` — actor `policy:<s>`, `refs` = the facts that satisfied the terms |
| | on a publication, where the binding says auto | intent · `uptake.decided` · `edge:<…>` — actor `policy:uptake` |
| **a person** | merges the release PR / runs `delivery approve` | intent · `approval.granted` · `stage:<s>` — role, via, rationale |
| **a person** | `delivery hold production-eu --until …` | intent · `hold.placed` · `stage:<s>` — expiry in payload |
| **a person, side door** | does something out of band and says so | intent · `breakglass.recorded` · `stage:<s>` — scope, action, from/to, incident, expiry |
| **a person** | `prod-rollback <sha>` / `delivery rollback production --to F417` | intent · `rollback.requested` · `stage:<s>` — the target freight, incident, via. A nomination, like `release.cut`: it makes the freight the stage's candidate; the policy's rollback terms decide, and write the same `promotion.decided` |
| **the tool's own check** | `prod-rollback` compares `migrations/` between the running and target SHAs | observation · `plan.summarized` · `stage:<s>` — the backward plan, actor `cli:prod-rollback`; read by the rollback direction's `plan_safe_or_approved` term |
| **a person** | takes up a published record on a gated edge, or approves one | intent · `uptake.decided` / `approval.granted` · `edge:<…>` — record version, role |
| **a bot's PR merge** | the bump PR for a by-version pin lands | intent · `uptake.decided` · `edge:<…>` — record version, the PR; the next `freight.discovered` carries the pin in its config |
| **whoever proposes a release across programs** | `pulumi delivery pin …` | intent · `release.pinned` · `release:<name>` — one member freight per program, an order, a reason |
| **executor** | starts / progresses / finishes an enactment | observation · `transition.started` / `.phase` / `.finished` · `transition:<T>` — outcome `succeeded`, `failed` (with step and error), or `abandoned` (refs the superseding decision); `resource.step` at whatever grain the executor emits. Under a strategy with steps, a phase carries the `step` index and the traffic `weight` |
| **program apply** | the stage declares how it is enacted | intent · `stage.declared` · `stage:<s>` — `strategy.steps`, a pause step carrying `terms` (the cutover gate inside the transition; `v_step_gate` evaluates it against facts since the rollout started) |
| **policy engine** | a step-gate verification fails, under a `rollback: auto` block | intent · `promotion.decided` · `stage:<s>` — back to the stable freight, refs the failed verification; the executor abandons the rollout citing it |
| **verification watch** | an external check concludes | observation · `verification.recorded` · `stage:<s>` — freight, check, outcome |
| **planner** | a preview runs (on candidacy, or ahead of it) | observation · `plan.summarized` · `stage:<s>` — counts, migrations touched, baseline |
| **planner** | a hyper-preview: the consumer against a proposed record | observation · `plan.summarized` · `stage:<s>` — with `against_record: {producer, version}`; read by the edge's `plan_safe_or_approved` term, never by the stage gate |
| **producer enactment** | a stack publishes outputs | observation · `output.published` · `record:<producer>` — versioned |
| **conformance watch** | on cadence | observation · `state.observed` · `stage:<s>` — what is actually running |
| **side jobs** | sentry marker, PR notifications | observation · `job.finished` · `stage:<s>` — with `optional` (see smells.md: that flag is really the program's to declare) |

Two doors, one destination: the merge that approves and the ECS console
click that rolls back both land as intent facts with an actor and a reason.
That is what lets the grid explain drift instead of fighting it — and what
lets the decision audit tell a policy-written promotion from one a person
typed in. A rollback has both doors too: the api-only ECS click on Monday
night is a break-glass fact explaining drift; Thursday's `prod-rollback`
run is a request, a backward plan, a confirmation and a policy decision —
the front door, with the tool's checks as the gate. Recorded as it runs
today (a person deciding, no policy), the same run is a `promotion.decided`
typed by a person, which the audit flags: today's rollback is a side door
with a gate in it.

Declarations that write no fact in this slice: `enact:` and `verify:`.
Their content shows up only through what executors and watches later
observe; whether the *declaration* of an executor or a check deserves a
subject of its own is open. `strategy:` is the one enactment detail that
does write a fact, because the tracker needs it: a pause step's terms are
what "paused, awaiting X" means, and only a declaration can say that.

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
(`transition.*`, `verification.recorded`, `plan.summarized`,
`output.published`). If their runs write those rows, this tracker renders
them unchanged. The intent rows come from people and policies, not from
executors — that boundary is the publication ≠ uptake discipline, and it's
what keeps evidence from quietly becoming normative.
