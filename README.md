# delivery-ledger

A fixture-driven slice of the ledger + tracker half of Delivery as Code:
one append-only table of attributed facts, and a release tracker that is
nothing but queries over it. Two scenarios render through the same views:
pulumi-service's own weekday release train, so the people who run that
pipeline can check it against what they know; and an invented two-team
estate — a platform program and a payments program with declared bindings
between them — so the multi-program mechanics have somewhere to bite.

Companion to the sketch in Claire's notebook
(`projects/delivery-as-code/poc-ledger-tracker.md`) and the thesis pair
(`thesis-frame.md`, `thesis-architecture.md`) — the kernel this exercises is
subjects × intent/observation facts × promotion × freight.

## Run

```
./scenarios/pulumi-service/fixture.py      # rewrite that scenario's facts.jsonl from its timeline
./render.py                                # lint, self-check, write out/pulumi-service/index.html
./render.py --scenario <name>              # another directory under scenarios/
./render.py --lint-only
./render.py --as-of 2026-08-24T16:20:00Z   # the ledger as it stood at that instant
./render.py --pure                         # views evaluated on demand (slow); must render the same page
```

Python 3 and its bundled sqlite3; no dependencies. Open `out/<scenario>/index.html`.

The views are the product; whether each one is evaluated on demand or
materialised once per build is a rendering choice. By default `render.py`
materialises every view in file order (views.sql is layered bottom-up), so
each layer is computed once from the one below instead of being re-derived
inside every correlated subquery above it — a full run of either scenario
takes well under a second. `--pure` keeps them as views: on pulumi-service it
renders the same page byte for byte (the check that nothing depends on the
materialisation); on multistack the trace views nest deeply enough to exceed
SQLite's limit on table references per statement, which is the sharper
statement of the same point (smells.md #18).

## What is in here

| file | role |
|---|---|
| `schema.sql` | the `facts` table (plus a one-row `clock`) — the only state |
| `views.sql` | every screen as a view: subjects → current state → gate terms → grid, lanes, trace, uptake, releases, audit — shared by every scenario |
| `render.py` | lint → self-checks (including mutation checks) → HTML; holds no state of its own |
| `scenarios/<name>/fixture.py` | a scenario as a timeline; emits `facts.jsonl` beside it so one story can't drift into two |
| `scenarios/<name>/facts.jsonl` | the ledger for that scenario (pulumi-service: 132 facts across two weeks; multistack: 143 facts across two programs) |
| `scenarios/<name>/scenario.py` | the scenario's page chrome, snapshot instants, and self-checks |
| `program.md` | the authoring side: what a delivery program *declares*, and which facts each declaration writes |
| `smells.md` | the acceptance log: anywhere a view wanted state the ledger doesn't hold, and what the slice taught |

## The five beats, and where they render

From the sketch's demo script:

1. **The subject grid** — stage × (should carry, carries, derived state); and
   **freight lanes** — freight × stage.
2. **Awaiting ≠ failure** — every waiting stage's gate, term by term, with
   the fact that satisfies each or the reason it doesn't; an open term is
   an open item with a duration, not a failed run.
3. **Where is my change** — PR → every freight that contains it → stages,
   as a join (membership is cumulative along master).
4. **A diff-gate** — production-eu's "auto if the plan is safe, else oncall"
   term against every freight: one auto, one gated-then-approved, one that
   would need approval — all read from plan facts, evaluable before the
   freight is even a candidate.
5. **Uptake, gated and auto** — two worker-pool bindings: the AMI edge
   (v13 published, v12 taken up, gated → "available, gated" with a
   preview) beside the image-reference edge (auto → the policy wrote the
   uptake decision the moment production published).

Plus: the side door (a break-glass fact explaining observed drift; drift
without one renders as UNEXPLAINED), Keith's release cards, the inside of a
transition (phase facts, resource steps, a failure with its step, an
abandonment citing the decision that superseded it), a decision audit, and
the ledger tail with every fact id linkable.

## The second scenario: two programs, no Uber program

`scenarios/multistack` is a platform program (stacks `network` and
`cluster`, team platform-eng) and a payments program (one stack, team
payments), each with its own dev → staging → prod, and no program that owns
the whole graph. Its story is a Kubernetes 1.31 upgrade that has to ship as
a pin-set across both teams. It adds, over the same views:

- **The estate** — programs × environments, assembled by joining each
  team's own stages on `environment`; each cell shows what the stage
  carries and what it is *wired with* (the version vector of records its
  by-reference edges have taken up).
- **Carried per (stage, stack)** — a stage carries a freight only when
  every stack its freight enacts does; between the network leg and the
  cluster leg the grid says `partial` with the per-stack detail, and a
  verification recorded in that window does not satisfy a downstream gate.
- **Both binding kinds, side by side.** Bindings are declared once per
  program as a pattern and instantiated per environment. A *by-reference*
  edge carries a per-stage record with its own uptake policy — typed terms
  on the edge, the same vocabulary as a stage gate — automatic within the
  platform team, and for payments ← cluster: auto in dev, auto-if-safe in
  staging, gated in prod. A *by-version* edge is a pin in the consumer's
  config: the platform bake publishes a base image, a bot PR bumps the pin
  and auto-merges on green checks, and the pin rides the payments train
  through every stage's ordinary gates (the real AMI shape from the first
  slice's review). "Pending" on a by-version edge means published but not
  pinned; where the pin has got to is a lanes question.
- **The hyper-preview as a fact** — the consumer's plan against a proposed
  record (`plan.summarized` with `against_record`), so an auto-if-safe
  uptake is evaluable at rest exactly as the diff-gate is. In staging the
  issuer rotation shows as a provider replace, is not safe, and waits for
  payments oncall; in prod the same preview sits behind an approval term.
- **A pin-set** — one `release.pinned` fact naming a member freight per
  program. It has no enactment of its own: each member moves under its
  owning team's policy, and the pin-set's state per environment is a join
  over the members' lanes.
- **Impact as a queue** — everything downstream of a producer along
  declared edges, transitively (network → cluster → payments), each hop
  with its uptake state right now.
- An uptake-decision audit mirroring the promotion one; a failed cluster
  leg retried by the executor against the standing decision (no second
  decision, nothing for the audit to flag).

Everything in it is illustrative: teams, stacks, PR numbers, timings and
error texts are invented to exercise the mechanism, not taken from a real
pipeline. The *shapes* come from the notebook's record: Tyler's VPC →
cluster → app split with no shared program and his "what's affected
downstream" question (log 2026-08-27-field-updates); the second customer's
"different resources have different lifecycles and different governance"
criterion for subject boundaries; Moderna's hyper-preview; and the
by-version AMI path the first slice's review surfaced (smells.md #17).

The page carries five snapshots of the same ledger: Wed 17:45 (F418 waiting
on oncall; production drifted since Monday night); Mon 16:20 (F417
mid-rollout, both versions live); Mon 16:50 (the EU diff-gate holding F417);
Wed 16:15 (F419 failed in testing-eu); Wed 16:21 (F420 superseding F419
mid-flight). The renderer only loads facts at or before the chosen instant —
the switch is a demonstration that state lives in the ledger, not the UI.

## Gate terms are data

A policy declares its gate as a list of typed terms —

```
verified{stage, check}   carried{stage}   approved{role}   not_held{}   plan_safe_or_approved{role}
```

— and `v_gate_term` evaluates each term for every (stage, freight) pair by
joining the relevant view: a verification counts only if it was recorded
after the stage carried the freight (you verify what ran); an approval
counts only with the required role; a hold fails the term for every freight
while it is active. `v_gate` is then *all terms satisfied*, with the first
unmet term as what the stage is waiting on and the latest satisfied term (or
the blocker's own onset — a hold's placement, a plan's timestamp) as when
the wait began. The human-readable `rule` on each policy is rendered from the
same terms by `fixture.py`, so text and evaluation can't disagree. These five
term types are, concretely, the vocabulary a gate-expression language would
need (architecture OPEN 4). Edges carry the same terms for
their uptake gate (`v_uptake_term`), evaluated against the latest published
record: `approved` is an approval on the edge for that record version,
`plan_safe_or_approved` reads the hyper-preview, `not_held` reads the
consumer stage. `verified` and `carried` name a stage and a freight, which
an uptake has neither of — on an edge they are undefined, and the view says
so rather than passing them.

## Discipline the fixture is held to

`render.py` refuses to render unless:

- every fact is well-formed (class ∈ {intent, observation}; kind belongs to
  its class; subject is `<type>:<name>`; consequential intent carries a
  rationale; observations don't; plan facts carry the fields the safe-rule
  reads; transition outcomes ∈ {succeeded, failed, abandoned});
- the file is append-only in time, ids are unique, `refs` point backward;
- subjects are declared before use (warehouse, stages, freight, edges);
  transitions start once and finish at most once;
- **no derived state is stored** — no `status`/`state` payload keys, no
  `awaiting`/`drifted`/`converged`/`held`/`superseded` values anywhere;
- every policy-written promotion decision passed its gate at the instant it
  was written (the ledger rebuilt at each decision's own timestamp);
- the scenario's self-checks pass (60 for pulumi-service, 58 for
  multistack), each pinning something a view must *include and exclude* at
  one instant — and eleven (ten) of them are **mutation checks**. For
  pulumi-service: the fixture is altered in memory (a verification moved before
  its deployment; a verification for a freight the stage never carried; an
  approval by the wrong role; a decision written by a person; an approval
  deleted and its ref stripped, on a gated stage and on an auto-if-safe
  stage; a second approval term added to a policy; a second active hold; a
  duplicate approval; the break-glass fact removed; a verification flipped
  to fail) and the views must say so. For multistack: a staging verification
  recorded between the network leg and the retried cluster leg; an uptake
  with its approval deleted, or written by a person, or approved by the
  wrong role; a preview flipped to safe; a hold on the consumer stage; the
  within-team auto uptake removed; the prod cluster leg left open; the pin
  removed from every freight; a payments build attributed to the platform
  warehouse. A SELECT cannot stay green with its mechanism broken.

The scenario keeps one figure in one place: the timeline in `fixture.py`
writes each timestamp once; counts and states in the page are derived.

## What is real and what is illustrative

Grounded in the repository (`doc/runbooks/reference/*.md`,
`.github/workflows/*.yml`, `cmd/prod-rollback` in pulumi-service):

- the stages — testing, testing-eu, staging, production, production-eu;
  there is no staging-eu;
- the release cron at 15:00 UTC on weekdays (`release.yml`; a second
  20:00 UTC cut Mon–Thu is not modelled), the `release/YYYY-MM-DD--HHMM`
  branch, the PR against `production` labelled `release`;
- staging deploying from the release PR the moment it opens, with no
  precondition (`pr-staging-deploy.yml`); the combined
  staging-deploy-and-integration-tests check (`staging-checks-pass`) as the
  branch-protection gate; the runbook's pre-merge checks (load-generator
  100%, smoke tests, ops channels);
- the merge as production's approval; production's integration tests; the
  "notify deployed PRs" job that may fail without failing the release; the
  ~20–30 minute ECS task roll; the five ECS services;
- rollback as reverting ECS task definitions per service, with a
  migration-safety check (`cmd/prod-rollback`), and per-service click-ops
  force-deploy as the rollback side door — the model for the break-glass
  fact (note: pulumi-service already uses "break-glass" for the HOTFIX
  merge-queue bypass, a different thing);
- that the API advertises `PULUMI_DEPLOY_DEFAULT_IMAGE_REFERENCE` and new
  deployment requests pick it up (rollback runbook, step 1), and that the
  workflow AMI embeds the workflow image and reaches the pool via launch
  template + instance refresh (rollback runbook). What is *not* grounded is
  drawing either as a stack-to-stack edge — see below.

The runbook (`pr-and-weekly-release-process.md`) lags the workflow files on
two of these — it still describes production-eu following production, and a
`release/staging@…` branch format — so a reader checking the fixture against
the runbook alone will find the runbook, not the fixture, out of date.

Illustrative, i.e. the thesis's construct rather than today's pipeline
(the multistack scenario is illustrative throughout — see above):

- **production-eu as a sequential, auto-if-safe follower of production.**
  Since 2026-07-14 `push-build.yml` deploys production and production-eu
  in parallel. The fixture's edge is a *proposed* policy — it is where Joe's
  "if migrations changed, require approval; otherwise fast-track" has an
  auto-follow edge to live on — and the policy description says so;
- **testing on every master build.** Today testing deploys pre-merge from
  the merge-queue SHA (`pr-build.yml`, currently disabled while the stack
  is restored) and testing-eu from `push-master.yml`; the fixture models the
  degenerate single-stage case;
- **content-keyed freight moving unchanged through the stages.** Today each
  stage rebuilds from the git SHA and production builds the merge commit,
  not master's head; the lanes caption says so. `state.observed` mapping ECS
  task-definition SHA tags to freight ids is the same simplification;
- the named phases inside the production transition (provisioning →
  both-live → cutover → retiring). ECS's rolling update passes through those
  states, but nothing records them; "a stack at two versions at once" is
  Keith's wish, not today's tooling;
- **both worker-pool bindings.** They are drawn as *by-reference*, per-stage
  edges — a versioned record published for a stage, taken up by that
  stage's consumer under its own policy (gated for the AMI, auto for the
  image reference). The real AMI mechanism is *by-version*
  (`update-workflow-ami.yml`, daily at 15:10 UTC): the bake opens a PR that
  pins the new AMI version into pulumi-service's config and enables
  auto-merge (`gh pr merge --squash --auto`), so the gate is the PR's
  required checks, not a reviewer; the program derives the default
  deployment image from the AMI's tag, so the uptake is a config change
  inside the freight boundary that rides the release train under every
  stage's ordinary gates. In the fixture's vocabulary that edge is
  *auto-with-checks*, not gated. The image reference is
  most likely a derivation from that same config, not a separate edge. The
  fixture keeps the by-reference pair because it exercises the uptake
  screen's mechanism (publication as evidence, uptake as intent, gated vs
  auto); the by-version shape — published → pinned by the PR → discovered
  as freight → lanes — is the next fact, in the multi-stack scenario where
  bindings are the subject (smells.md #17);
- the Wednesday-afternoon failure (an ECS circuit breaker in testing-eu) and
  the supersession (F420 landing while F419 was still rolling) — plausible,
  not from the record;
- all people, PR numbers, run ids, digests. Run #418 / PR #14882 are borrowed
  from Vic's `SERVICE_RELEASE` fixture so the two are recognisable side by
  side.

## Keith parity

Keith's tracker (`releases.pulumi-dev.io`) answers these by mining CI logs
and commit subjects. Status here:

| tracker feature | here |
|---|---|
| current release train + stepper | grid row + lanes column for the cut freight |
| trace: `queued` | not modelled — the merge queue is a GitHub object before the warehouse; a watch could write `queued` observation facts |
| trace: `unreleased` | becomes "in testing since T" — testing is a stage |
| trace: `released` | lanes cell per stage, with the enactment time and the freight that carried it there |
| trace: `open` / `external` | not modelled (PR-state and cross-repo resolution are GitHub-side) |
| past releases (cut, merged, deployed, PRs) | `v_releases` + `v_release_prs` (PRs since the previous cut) |
| deploy failure: failed step + deep link | `transition.finished` with `outcome: failed`, `failed_step`, `step_url`, `error` — rendered in the grid and the transition detail |
| in-flight step + typical duration | `transition.phase` carries the current phase; typical durations would be a query over prior transitions (not built) |
| live `pulumi up` resource progress | `resource.step` facts on the transition (three shown); the full stream is the same shape at higher volume |
| head SHA vs merge-commit SHA | not distinguished — see "content-keyed freight" above |
| "yours" filter | client-side over `author` — unchanged |
| cut / close release buttons | intent doors: `release.cut` is a fact; a button would write one; `release.closed` is not modelled |

Keith's own list of what DaC would need to provide (from the tracker
investigation): a stage-progression entity → `transition` + `promotion.decided`;
membership → `freight.prs` + `v_membership`; live progress → transition
facts; status webhooks → the ledger is the event stream; pipeline definition
as queryable state → `stage.declared` + `policy.declared` (terms as data);
cross-repo tracing → PR entries would carry a repo when foreign (not
exercised); where-is-my-change as a product query → `v_trace_summary`.

## Boundaries, on purpose

No pump, no executors, no engine changes (Tyler's and Florian's lanes). No
ingest — the facts are generated from a hand-written timeline; the cheapest
real ingest (Tyler's POC events, deployment webhooks, or a watch over
GitHub) is a later layer. Real output-record publication is blocked on the
ESC pulumi-stacks provenance edge, so every published record here stands
in. The pin-set declares an order for its members and nothing enforces it;
a cycle in declared edges only stops the impact walk, it is not flagged;
by-version pins compare integer versions, not semver. Tables, not chrome:
the grid vocabulary (glyph + colour, never colour alone) is borrowed from
Vic's DeliveryUX plan.
