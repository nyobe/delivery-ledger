# delivery-ledger

A fixture-driven slice of the ledger + tracker half of Delivery as Code:
one append-only table of attributed facts, and a release tracker that is
nothing but queries over it. The scenario is pulumi-service's own weekday
release train, so the people who run that pipeline can check it against
what they know.

Companion to the sketch in Claire's notebook
(`projects/delivery-as-code/poc-ledger-tracker.md`) and the thesis pair
(`thesis-frame.md`, `thesis-architecture.md`) — the kernel this exercises is
subjects × intent/observation facts × promotion × freight.

## Run

```
./render.py              # lint the fixture, run the self-checks, write out/index.html
./render.py --lint-only
./render.py --as-of 2026-08-24T10:20:00Z   # the ledger as it stood at that instant
```

Python 3 and its bundled sqlite3; no dependencies. Open `out/index.html`.

## What is in here

| file | role |
|---|---|
| `schema.sql` | the `facts` table (plus a one-row `clock`) — the only state |
| `facts.jsonl` | the fixture: 107 hand-written facts across two weeks of the pipeline |
| `views.sql` | every screen as a view: subjects → current state → candidate/gate/drift → grid, lanes, trace, uptake, releases |
| `render.py` | lint → self-checks → HTML; holds no state of its own |
| `program.md` | the authoring side: what a delivery program *declares*, and which facts each declaration writes |
| `smells.md` | the acceptance log: anywhere a view wanted state the ledger doesn't hold |

## The five beats, and where they render

From the sketch's demo script:

1. **The subject grid** — stage × (should carry, carries, derived state); and
   **freight lanes** — freight × stage.
2. **Awaiting ≠ failure** — production's gate evaluated at rest: three
   verifications met, one approval open, rendered as an open item with a
   duration, not a failed run.
3. **Where is my change** — PR → freight → stages, as a join.
4. **A diff-gate** — production-eu's "auto if the plan is safe, else oncall"
   rule against three freights: one auto, one gated-then-approved, one that
   would need approval — all readable from plan facts.
5. **A pending uptake** — the worker-pool AMI edge: v13 published, v12 taken
   up, policy gated → "available, gated" with a preview.

Plus: the side door (a break-glass fact explaining observed drift), Keith's
release cards, the inside of a transition (phase facts, resource steps), and
the ledger tail with every fact id linkable.

The page carries three snapshots of the same ledger (Wed 11:45; Mon 10:20
mid-rollout; Mon 10:50 with the EU diff-gate live). The renderer only loads
facts at or before the chosen instant — the switch is a demonstration that
state lives in the ledger, not the UI.

## Discipline the fixture is held to

`render.py` refuses to render unless:

- every fact is well-formed (class ∈ {intent, observation}; kind belongs to
  its class; subject is `<type>:<name>`; intent-with-consequence carries a
  rationale; observations don't);
- the file is append-only in time, ids are unique, `refs` point backward;
- subjects are declared before use (stages, freight, edges), transitions
  start once and finish at most once;
- **no derived state is stored** — no `status`/`state` payload keys, no
  `awaiting`/`drifted`/`converged`/`held` values anywhere in a payload;
- 22 self-checks pass, each pinning something a view must *include and
  exclude* (an approval on production must not count for production-eu; F417
  must be awaiting nowhere; the EU edge must not be pending; F418 must not
  exist at Monday 10:20) — so a SELECT cannot stay green with its mechanism
  broken.

The scenario keeps one figure in one place: counts and states in the page are
derived, never typed twice.

## What is real and what is illustrative

Grounded in the runbook (`doc/runbooks/reference/pr-and-weekly-release-process.md`
and neighbours in pulumi-service):

- the stages and their order — testing/testing-eu on every master push;
  staging from the release PR; oncall reviews staging (load-generator 100%,
  smoke tests, integration tests) and merges; production, then
  production-eu after production's integration tests;
- the merge as the approval; the release cron; `release/staging@MMDDYYYY-HHMM`
  branch names; the "notify deployed PRs" job that may fail without failing
  the release; the ~20–30 minute ECS task roll;
- rollback as reverting ECS task definitions per service, with a
  migration-safety check (`cmd/prod-rollback`), and click-ops per-service
  force-deploy as the emergency path — the model for the break-glass fact;
- the workflow AMI embedding the workflow image and reaching the worker pool
  via launch template + instance refresh (rollback runbook, "Rolling back to
  a previous workflow-ami version").

Illustrative:

- the named phases inside the production transition (provisioning →
  both-live → cutover → retiring). Today's ECS rolling update passes through
  those states, but nothing records them; "a stack at two versions at once"
  is Keith's wish from the braindump, not today's tooling;
- the forward direction of the AMI edge as a *gated uptake with a versioned
  record* — inferred from how rollback walks it; pulumi-service-ami-rotate
  wasn't read for this;
- the diff-gate policy on production-eu. Today EU follows US
  unconditionally; the rule here is the answer to Joe's "if migrations
  changed, require approval" thread, applied where the pipeline has an
  auto-follow edge to put it on;
- all people, PR numbers, run ids, digests, timestamps. Run #418 / PR #14882
  are borrowed from Vic's `SERVICE_RELEASE` fixture so the two are
  recognisable side by side.

## Keith parity

Keith's tracker (`releases.pulumi-dev.io`) answers these by mining CI logs
and commit subjects. Status here:

| tracker feature | here |
|---|---|
| current release train + stepper | grid row + lanes column for the cut freight |
| trace: `queued` | not modelled — the merge queue is a GitHub object before the warehouse; a watch could write `queued` observation facts |
| trace: `unreleased` | becomes "in testing since T" — testing is a stage |
| trace: `released` | lanes cell per stage, with the enactment time |
| trace: `open` / `external` | not modelled (PR-state and cross-repo resolution are GitHub-side) |
| past releases (cut, merged, deployed, PRs) | `v_releases` |
| deploy failure: failed step + deep link | transition facts carry the run; a failed `transition.finished` renders as `failed`; step-level deep links are payload |
| live `pulumi up` resource progress | `resource.step` facts on the transition (three shown); the full stream is the same shape at higher volume |
| "yours" filter | client-side over `author` — unchanged |
| cut / close release buttons | intent doors: `release.cut` is a fact; a button would write one |

Keith's own list of what DaC would need to provide (from the tracker
investigation): a stage-progression entity → `transition` + `promotion.decided`;
membership → `freight.prs`; live progress → transition facts; status
webhooks → the ledger is the event stream; pipeline definition as queryable
state → `stage.declared` + `policy.declared`; cross-repo tracing → PR entries
carry a repo when foreign (not exercised); where-is-my-change as a product
query → `v_trace`.

## Boundaries, on purpose

No pump, no executors, no engine changes (Tyler's and Florian's lanes). No
ingest — the facts are typed by hand; the cheapest real ingest (Tyler's POC
events, deployment webhooks, or a watch over GitHub) is a later layer. Real
output-record publication is blocked on the ESC pulumi-stacks provenance
edge, so the AMI record stands in. Tables, not chrome: the grid vocabulary
(glyph + colour, never colour alone) is borrowed from Vic's DeliveryUX plan.
