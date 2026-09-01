# Design smells

The acceptance test from the sketch: *if a view needs its own mutable state,
that's a design smell worth recording.* Everything here was noticed while
writing `views.sql` and `render.py` against the fixture, or surfaced by the
verification pass over the first slice. Dated entries; a smell that gets
resolved says how.

## 2026-09-01 — first slice, and its revision

**No view needed a second mutable table.** Every screen — grid, lanes,
gates, trace, diff-gate, uptake, drift, releases, transition detail, audit —
is a SELECT over `facts`. Recorded as the positive result the slice was
built to test.

What the slice taught, honestly:

1. **`clock`** — a one-row table holding "now". Holds have an expiry and
   "active" is relative; the renderer sets the clock. This is the one piece
   of non-ledger state and it is an *input*, not stored status. Letting the
   caller set it is what makes time-travel rendering possible. Keep.

2. **Gate rules as text → gate terms as data (resolved).** The first slice
   carried each policy's rule as a string and evaluated it in a hand-written
   per-stage `CASE`; the rendered gate screen re-listed the terms by hand,
   and the two drifted (the diff-gate screen ignored the carry and hold
   terms the SQL applied). Now the policy fact carries `terms` — a JSON list
   of typed terms — and `v_gate_term` evaluates every term for every
   (stage, freight) by joining the view each type names. The residue is one
   `CASE` per *term type* (five types), which is exactly the surface a
   gate-expression language (OPEN 4) has to cover. The `rule` text is
   rendered from the terms by `fixture.py`, so it can't disagree.

3. **Pseudo-stages.** The warehouse is now a declared subject
   (`warehouse:master`), but `release-train` is still a sentinel string in
   `v_candidate`. The release train is a real subject in this pipeline
   (Keith's cards are keyed on it) that the slice models only as a
   `release.cut` fact on the freight. If a train grows behaviour — carrying
   several freights over its life, or being closed without shipping — it
   wants to be a subject.

4. **`v_carried` privileges `stack = 'service'`.** The grid is per stage,
   but production hosts three stacks. The service stack is what the release
   train moves, so the grid follows it; the two worker-pool edges render
   separately. A stage-with-many-stacks grid needs (stage, stack) cells and
   a per-stack "carried" — which is the multi-stack scenario's first
   demand. *(Resolved 2026-09-01: `v_carried_stack`, and a `partial` state
   when the stacks disagree — the warehouse declares which stacks its
   freight enacts.)*

5. **`state.observed` reports freight ids.** The conformance watch says
   `api: F416`; in reality it reads ECS task-definition SHA tags (what
   `prod-rollback` matches on) and would have to *map* them to freight
   through the freight's source SHA — a hidden join done in the watch. The
   honest version is a content key on the deployed artifact (the OCI digest
   in the task definition) so the watch reports what it saw and the ledger
   does the join. Same gap the architecture names for ESC (no lock file, no
   captured component versions), and the same one behind "today each stage
   rebuilds from the git SHA": until the artifact is the key, "the testing
   build is the release" is a claim about the thesis, not the pipeline.

6. **`id` and `refs` are columns, not payload.** Promoted on day one rather
   than after a JOIN became unreadable: every policy decision cites the
   facts that satisfied its terms, the audit reads `refs`, and the page
   links fact ids everywhere. The deviation earned its place immediately.

7. **Approval scope.** An approval is (stage, freight, role). It does not
   carry across stages (a self-check pins it) and the wrong role does not
   count (a mutation check pins it). Whether one approval should be able to
   cover a *composite* — "ship F418 to US and EU" as one decision — is the
   pin-set release question; this slice keeps approvals per subject.

8. **A verification must postdate the enactment it verifies.** The
   verification pass moved a staging verification to before the staging
   deploy finished and the first slice's gate happily counted it. The
   `verified` term now requires the verification's timestamp to be at or
   after the stage's first successful carry of that freight, and the unmet
   text says why ("recorded before staging carried F418 — re-run"). Mutation
   check pins it.

9. **Membership is cumulative.** `freight.prs` lists what a build
   *introduced*; a PR merged to master is in every later master build, so
   where-is-my-change joins through `v_membership` (cumulative along the
   branch), and a release card's PR list is "introduced since the previous
   cut" (`v_release_prs`). The first slice read `prs` as total membership
   and would have said a PR wasn't in a later freight that plainly
   contained it.

10. **`optional` on `job.finished` is intent living in an observation.**
    Whether a side job may fail without failing the release is the
    program's call, not the job's report. It should be declared (a side-job
    declaration on the stage) and the observation should carry only the
    outcome. Left in the payload for now; the transition detail renders it.

11. **The ledger records what it is told; the audit tells you what to
    make of it.** A rogue `promotion.decided` typed by a person, or a
    policy decision with no approval on record, is a valid fact. Nothing in
    the schema can forbid it, and nothing should — the side door depends on
    that. What the ledger owes is a *query* that distinguishes it:
    `v_audit_flag` checks every decision against the policy that should
    have written it — actor is the stage policy; every approval-bearing
    term (`approved`, and the approval half of `plan_safe_or_approved`)
    was met on record at decision time; evidence cited. The second
    verification pass caught the first version reading only `approved`
    terms, which left production-eu unauditable and a second approval
    role invisible; `v_audit_term` now iterates the terms. Four mutation
    checks pin it.

15. **"As of T" is only half-expressible in SQL here.** The audit checks
    approval-bearing terms at decision time because those reduce to
    "was a fact with `ts <= T` on record". The other terms — verified,
    carried, not-held — depend on "latest" views (`v_carried`,
    `v_verified`, `v_hold_active`) that are latest *as of the clock*, not
    as of an arbitrary T. Replaying the whole gate at decision time is
    done by `render.py` (rebuild the ledger prefix at each policy
    decision's own timestamp), not by a view. A ledger that wants to
    answer "did this gate pass when it was decided?" *as a query* needs
    its per-subject state parameterised by T — bitemporal views, or a
    clock per row. Real design point for ledger residence (OPEN 5): the
    engine's journal already is a prefix-replayable log; a status store
    is not.

16. **The pending-uptake preview is a string, not a fact.** `v_pending_uptake.preview`
    concatenates "preview consumer with key = value". The honest artefact is a
    `plan.summarized` for the consumer against the proposed record —
    Moderna's hyper-preview — which would make the uptake gate evaluable
    at rest exactly as the diff-gate is. *(Resolved in the multistack
    scenario: `plan.summarized` with `against_record`, read by the edge's
    gate — see #22.)*

17. **Two kinds of binding, and the fixture drew the wrong one for the
    AMI.** Claire's read of the real mechanism (2026-09-01): the bake runs
    periodically; when it finishes it opens a PR that pins the new AMI
    version into pulumi-service's config — the PR is the gate — and the
    program derives the default deployment image from the AMI's tag. One
    refinement from the workflow file: the bake enables auto-merge on that
    PR (`update-workflow-ami.yml:99`, `gh pr merge --squash --auto`), so
    the gate is the PR's required checks rather than a person. So the AMI
    is a **by-version** binding whose uptake is a config change inside the
    freight boundary, taken up automatically on green CI: it rides the
    release train and meets every stage's gate on the way. The fixture's `workflow-pool@production ←
    workflow-ami@production.ami_id` is a **by-reference**, per-stage
    binding with its own gated uptake — the other legitimate kind (P4:
    by-version vs by-reference is an explicit per-edge choice), but not
    today's. The image-reference edge is probably not a separate edge
    either: the API advertises a value the same program derived from the
    same config. In the ledger the faithful shape is `output.published`
    (bake) → `uptake.decided` by the PR merge → `freight.discovered` whose
    config pins the version → ordinary lanes; "pending" = published but in
    no freight yet, and "where is ami v13" is a trace question. Bindings
    are the multi-stack scenario's core; model both kinds there, side by
    side. *(Done: the multistack scenario's by-version pin is this shape —
    #24.)*

12. **Latest-wins is by arrival, not by clock.** Two facts can share a
    timestamp; every "latest" view now dedups by `seq`. The verification
    pass produced a duplicate row per stage with two approvals at the same
    instant, and a fanned-out grid with two active holds — holds are now
    aggregated to one row per stage (the later expiry wins; both holders
    shown).

13. **Cost.** The verification pass measured the lanes/trace views at
    ~15 s and ~47 s per query with 100 synthetic freights (607 facts), from
    ~4 ms and ~15 ms at 132 — the CROSS JOIN of freight × stage over
    correlated subqueries into `v_transition` is super-linear. Not fixed:
    at fixture scale it is invisible, and the fix (materialise the
    per-subject layer per render, or index the derived tables) is a
    rendering concern, not a ledger one. Worth carrying into OPEN 5 as a
    data point: Supabase-scale is 21k updates per 30 days.

14. **Grounded-vs-proposed lives in the policy, not just the README.**
    The production-eu policy description says it is a proposal (today EU
    deploys in parallel with US). Where a fixture departs from the pipeline
    it claims to model, the departure should be legible in the fact, not
    only in a document beside it.

## 2026-09-01 — the multistack scenario

**Still no second mutable table.** Six new screens (estate, both binding
kinds, hyper-preview, pin-set, impact, uptake audit) over two programs, and
every one is a SELECT over `facts`. Same result as the first slice; recorded
again because the second fixture was chosen to break it.

18. **The per-subject layer is a materialisation boundary, not a speed-up.**
    Generalising `v_carried` to per-(stage, stack) multiplied the cost of
    every correlated reference to it, and the trace views nest five levels
    of those: a pulumi-service render went from seconds to minutes. The
    renderer now materialises every view once per build, in file order,
    and `--pure` keeps them as views to prove nothing depends on it. It
    proved two things. Every view pure mode can evaluate returns the
    same rows, as a set, as the materialised build (`--pure-check`) —
    after fixing one `group_concat` whose order differed between a
    table scan and a view, which the earlier whole-page comparison
    caught and a set comparison would not; scan-order differences in
    views without an ORDER BY remain unproven either way. And `v_trace_summary` cannot be evaluated pure at all, on either
    scenario: SQLite refuses the statement (more than 65 535 table
    references once the views are inlined) — 61 of 62 views agree, one is
    unreachable without the boundary. The layering in views.sql — subjects → current
    state → derived → screens — is where a status store would sit; the
    ledger stays the only source, and the store is a cache the views
    define. That is the shape OPEN 5 should assume.

19. **Carried-since is not last-enacted-at.** A record uptake re-enacts the
    consumer stack with the same freight; the first version of
    `v_carried_stack` took the latest success as "since", so payments/
    staging read as carrying A231 "since 11:40" after its record uptake,
    and payments/prod's awaiting-since jumped with it. `since` is now the
    first success of the current freight after the last success of any
    other; `last_enacted_at` is kept beside it. Two clocks, both honest,
    and the grid wanted the first.

20. **Two of the five term types have no meaning on an edge.** Uptake is a
    promotion on the edge, so its gate reuses the term vocabulary —
    `approved` (an approval for that record version), `not_held` (the
    consumer stage), `plan_safe_or_approved` (the hyper-preview). But
    `verified` and `carried` name a stage and a freight, and an uptake has
    neither; `v_uptake_term` labels them undefined rather than passing
    them. For OPEN 4: the language needs a notion of which terms are
    well-typed on which subject, not one flat vocabulary.

21. **Patterns are expanded by the fixture, not by SQL.** A binding is
    declared once per program and instantiated per environment; the
    instances carry a `pattern` reference and the views read instances
    only. That kept `v_edge` unchanged for the first scenario and the
    "same pair, re-instantiated per stage" story explicit in the facts. The
    cost is that the interface/resolution split (P4) lives in the apply,
    which is where it belongs — but a program that adds an environment
    must re-apply for the new instance to exist.

22. **The hyper-preview is a fact now, and it needed its own lane.** A
    `plan.summarized` with `against_record` on the consumer stage would
    have replaced the stage's own promotion plan in `v_plan` (latest wins
    per stage × freight). Stage plans and record previews are now two
    views over one kind, split on the presence of `against_record`. Same
    kind, different question; the payload field is the discriminator.

23. **A pin-set is a join with no enactment.** `release.pinned` names one
    freight per program and an order. Its per-environment state is
    complete/partial/pending over the members' lanes, and nothing enforces
    the order or coordinates a cutover — the architecture's "the composite
    transition coordinates cutover ordering" has no fact to stand on yet.
    What the fixture does show: the pin-set is legitimately written by
    someone outside both programs, and each member still moved only under
    its own team's decision.

24. **"Pending" on a by-version edge is a membership question.** The pin is
    in the consumer's config, so published-but-not-pinned is the only
    edge-level state; after the bump PR the question is "which freight
    carries the pin and where is it" — `v_pin_stage` joins lanes. The
    uptake decision and the config pin can disagree (a mutation check
    removes the pin from every freight): the view says `unpinned` per
    stage while the edge says pinned. Two facts, one from a PR and one
    from a build; neither is derived from the other, so the inconsistency
    is visible rather than hidden.

25. **What a stage is wired with is the consumed-version vector.** The
    estate cell's `wired: cluster_endpoint v3` is `v_pending_uptake`
    filtered to the consumer stage — the version vector of records it has
    taken up, as data. The architecture's "what is staging actually wired
    with" (2026-08-31-output-records) is that column.

26. **Retry is the executor's business.** The failed cluster leg in
    staging is retried by a second transition citing the same standing
    decision; no second `promotion.decided` is written, so the audit has
    nothing to flag and the grid moves failed → in-flight → converged. A
    retry that wrote a person's decision would be flagged as a person's
    decision, which is right: the intent did not change.

27. **`pending_downstream` is computed by string surgery.** The estate
    counts pending uptakes whose producer's stage is this stage by
    splitting `producer` on `@`. The producer of a record should be a
    (stack, stage) pair in the fact, not a string the view parses.

28. **The impact walk guards against cycles by not revisiting a node; it
    does not flag the cycle.** Declared edges can be checked for cycles
    statically (the architecture says so); no view does it yet.

29. **`release-train` is program-scoped now, and still a sentinel.** The
    candidate for a stage whose upstream is the release train is the
    latest cut freight *of that stage's program*; the sentinel string
    survives from #3.

30. **The stack is grip-granularity content that never became a subject.**
    `v_carried_stack` is per (stage, stack), the warehouse declares the
    stacks its freight enacts, transitions name their stack — but `stack`
    is a payload string, not a declared subject with facts of its own. P5
    says subjects come at the size you operate on; the scenario operated
    on stacks (a failed leg, a partial rollout, a per-stack record) with
    no subject to hang those on. Whether the stack is the subject and the
    stage a grouping, or the reverse, is a real call the fixture ducked.

31. **An enactment's claim about what it consumed is unchecked.** A
    transition carries `record_version` as a display field; nothing joins
    it to the record actually published or taken up at the time. A cluster
    leg claiming to have run against network v1 after v2 was taken up
    renders as converged. The honest view would compare the enactment's
    version vector to the edge's consumed versions at its start — the
    conformance join, one level down.

32. **Concurrent legs.** A multi-stack promotion can have two legs open at
    once; `v_inflight` keeps one row per stage (the grid joins on it) but
    now counts and names every open leg. The first version showed only the
    latest, which is the single-stack assumption in one more place.

## 2026-09-01 — rollback by the front door (pulumi-service, Thursday)

**Still no second mutable table.** A rollback request, a backward plan, a
policy decision in the other direction, a freight carried twice, and every
screen still a SELECT over `facts`. What it cost the views, honestly:

33. **Consent lapses when intent moves on.** The first version of the gate
    let F418's standing approval re-promote it the instant the rollback
    landed, and let F417's Monday approval wave Thursday's rollback through
    with nobody confirming. The rule that stops both: an approval for
    (stage, freight) counts only if given after the latest decision that
    moved the stage off that freight (`v_intent_moved`). It is an
    intent-class floor — decisions, not enactments — deliberately: an
    enactment-based floor ("withdrawn when another freight succeeded") has
    a hole for a freight that was decided and aborted before it ever ran,
    which is exactly the canary-abort case. Verifications keep their
    observation-class floor (recorded after the first carry) and do not
    lapse on a reversal: evidence about how a freight behaved does not
    expire when consent does. Plans get a third rule (#37). Three floors,
    three fact kinds, each floored on the kind of fact it is about — worth
    stating that way in OPEN 4.

34. **Direction is a relation, not a field.** A pair (stage, freight) is
    in the rollback direction when the freight was discovered before the
    stage's incumbent (`v_direction`); the policy may declare a `rollback`
    block and a stage without one gates rollbacks with its ordinary terms
    (production-eu does, and its follower terms are the right ones). The
    decision fact carries nothing about direction — the audit derives the
    direction the decision *had at that instant* from the same two facts
    (`v_audit_ctx`), so a person cannot claim the forward gate for a
    backward promotion. The ambiguity the derivation leaves: a stage whose
    stacks disagree has no carried freight, so the incumbent falls back to
    the latest success on any stack.

35. **Rollback needs a nomination, not a decision kind.** Forward, the
    candidate comes from upstream; a rollback target comes from history,
    and nothing upstream ever offers it. `rollback.requested` is that
    nomination — the only forward analogue is `release.cut`, which
    nominates a freight for the train — and the decision it leads to is an
    ordinary `promotion.decided`. The mutation that removes the request
    shows the split: the rollback gate for F417 still evaluates at rest,
    but nothing makes F417 the candidate. Whether the request should
    instead be an approval that doubles as a nomination was considered and
    rejected: the auto-if-safe branch (no migration) needs no approval and
    would then have no nomination either.

36. **Front door for the ledger, side door for the engine.** `prod-rollback`
    swaps task definitions with `UpdateService`; no Pulumi update runs.
    The ledger records a proper decision and a successful transition with
    `ops_update` NULL; the conformance watch reads ECS and finds it matches
    intent; and the engine's state still describes F418 — the next
    `pulumi up` would re-deploy it. The grid now carries `engine_freight`
    (the freight of the latest success that ran a Pulumi update) and warns
    when it differs from what runs. That is a drift ECS cannot see and the
    ledger can, and it is Keith's two reconciliations in one row: world →
    intent is clean, state → world is not.

37. **A plan is against a world.** The first floor tried ("a plan must
    postdate the stage's current carried-since") marked the incumbent's
    own pre-enactment plan stale, since every plan precedes the carry it
    leads to. The honest rule: a plan for F is stale once the stage has
    carried some *other* freight since it was computed. It changed one
    Wednesday check: production-eu's plan for F416 (Aug 18, against F415)
    now reads `stale` rather than `auto` — correct, since a rollback to
    F416 would need a fresh plan against F417. Stale is a fourth term
    outcome beside auto / approved / open / no-plan.

38. **The lookback is a term that reads the clock.** `previously_carried
    {within_hours: 120}` is `prod-rollback`'s task-definition window as a
    gate term; after `not_held` it is the second term whose answer depends
    on `now`: while F417 is production's rollback candidate on Thursday,
    the term is satisfied until Fri Aug 29 16:31 (120h after production
    last carried it) and unmet a second later, with no new fact.
    Time-dependent terms are the reason the clock is an input (#1) and
    the reason "as of T" is only half-expressible (#15).

39. **Passing through is history; reversal is not.** The first lanes
    version marked every freight the stage had moved off as withdrawn,
    which made F416's ordinary journey look like an incident. `v_reversal`
    is intent moving from a freight to an *older* one, and only that
    renders `rolled-back` (whether the freight had been carried or was
    still in flight). One derivation, read by lanes, the grid, the trace
    and the release cards. F417, reached twice, shows its first arrival
    and "again Thu 11:14" from `v_carried.since` — the carried-since rule
    from #19 already had the second stint right.

40. **A change is in a stage iff the stage's freight contains it.** The
    trace cell's `reached` was ever-reached; after a rollback that hides
    the fact that #46173 (the INC-2311 fix) left production. `is_current`
    on a trace cell is now derived from cumulative membership joined to
    what the stage carries, and the summary line names the stages a
    change was rolled back from. Both scenarios' existing trace checks
    held; the rollback is what made the distinction visible.

41. **The rollback re-exposes a fixed bug, and no view says so.** Rolling
    production back to F417 brings back the rate-limit collision F418
    fixed; the request's rationale says it, and the trace shows #46173
    rolled back, but nothing connects INC-2311 to the PR that closed it
    and hence to "INC-2311's fix is no longer in production". An incident
    would be a subject with facts of its own; not modelled here.

42. **`rollback.requested` has no retraction.** A request is open until
    the next decision on the stage answers it. A person who changes their
    mind writes nothing that closes it; the candidate stays the requested
    freight until some decision lands. Cheap to add (`rollback.withdrawn`,
    or a request naming no freight); left out until a fixture wants it.

## 2026-09-01 — the canary (multistack, Wednesday)

**Still no second mutable table.** Both-live, a gate inside a transition, an
automatic abort, and every screen a SELECT. What the canary cost, and what
it settled:

43. **A stage carries a set for as long as a transition is open.** `v_carried`
    stayed what it was — the latest success — and `v_live` is the set:
    carried (stable) plus in flight (canary), with the weight the executor
    last reported. The grid's "carries" column did not need to change;
    "live" is a second column. That is the cheapest reading of "a stack
    should exist at two versions at once": the transition already held both
    versions (the F417 ECS rollout's `both-live` phase said so on day one);
    what was missing was a view that reads them out as a set with shares.

44. **Conformance is against the enactment, not against intent.** The
    first `v_observed` compared each observed value to the desired freight.
    Inside the pause that reads the stable ReplicaSet (A231) as drift from
    intent (A232) — false, and UNEXPLAINED, since no side-door fact exists.
    It also reads a decided-but-not-enacted stage as drifted. `v_observed`
    now compares to `v_live`: what the enactments say should be there. The
    intent-vs-enactment gap is the grid's status column (pending, in-flight),
    not drift. The pulumi-service checks held under the change: Monday's
    api@F416 is drift either way, because F416 is neither carried nor in
    flight.

45. **The cutover is a gate inside the transition, and it is declared.** A
    pause step carries terms in the stage-gate vocabulary, evaluated with
    one more floor — facts recorded since this rollout started — so the
    approval that opened the stage gate does not also promote the canary,
    and an analysis from before the rollout does not count. This is the
    "transitions as the blue/green primitive" position with something under
    it: the executor pauses on an observation (`phase: paused, step: 1`)
    and the ledger says what would let it continue. Three terms of six are
    defined on a step (`verified`, `approved`, `not_held`); the rest name
    stages, freight or plans a step has no business with — OPEN 4's terms
    are typed by subject three ways now (stage, edge, step).

46. **The step's declaration lives on the stage.** `strategy.steps` is a
    payload on `stage.declared`, the first enactment detail to write a fact
    (program.md said `enact:` writes none). It has to: "paused, awaiting X"
    is meaningless without the declaration that says what X is. Whether the
    strategy is the stage's or the executor's to declare is a real call;
    here the stage owns it because the tracker reads it.

47. **An abort is a reversal, and needs its own gate.** Intent returning to
    the stable is a promotion to a freight older than the one decided —
    `v_direction` now compares to *desired*, not to what is carried, so an
    abort while the canary was never carried is the same relation as
    Thursday's rollback. Under the forward gate the abort would fail (A231's
    approval lapsed when A232 was decided — the rule from #33 biting
    correctly, in the wrong direction); prod's rollback block (`auto`,
    `previously_carried`) is what lets the policy write it with nobody
    involved. The mutation that removes the block shows the audit flag.
    Argo's `abort` needs no approval; here that is a policy choice the team
    declares, not a property of aborts.

48. **The decision-replay check must exclude the decision.** The
    per-decision self-check built the ledger at the decision's own
    timestamp — including the decision — so the abort read as *forward*
    (desired already A231) and failed the wrong gate. The ledger "as it
    stood when the decision was written" is everything up to that instant
    except the decision itself, and both scenarios' harnesses now say so.
    The audit view (`v_audit_ctx`) never had the problem: it reads the
    previous decision's freight explicitly. Same "as of T" seam as #15,
    one row sharper: inclusive and exclusive of the fact being judged are
    different questions.

49. **The abort's evidence is a verification that failed.** The failing
    `canary-analysis` is an ordinary `verification.recorded` on (prod,
    A232); the policy's decision cites it; nothing new was needed. What is
    missing is the policy *trigger* as data — "on a failed step-gate
    verification, decide the stable" is prose in the rollback block's
    description, and the policy engine's business, as every trigger here
    is.

50. **Blue/green in pulumi-service was not built, on purpose.** The F417
    ECS rollout's phases (provisioning → both-live → cutover → retiring)
    are the same shape without weights or a declared pause; `v_live` reads
    them as a set already (in flight, no weight), and `v_observed` would
    accept either version during the window. Argo's shape has the pause
    and the analysis, which is what exercises the step gate; ECS's does
    not, today. One canary, aborted, was enough to bake or break the
    position; a promoted one is a mutation check (`m_promote`), not a
    story.

51. **Pin-set re-pin was not modelled.** "Rollback of a pin-set = re-pin"
    (threads) is a second `release.pinned` naming older members, and the
    members then move under their own rollback blocks. Cheap, but this
    week's story had no reason to unpin k8s 1.31; left for a fixture that
    does.

## 2026-09-01 — verification pass 4 (rollback and canary views)

47 agents over the new mechanisms only (`verification/2026-09-01-pass-4.txt`):
35 findings, 30 confirmed. The escapes were all one class — ordering by
clock where the ledger's order is arrival — plus three shapes the fixtures
had not exercised (a rollback of a rollback, a multi-stack stage between
legs, a freight one stack ran). Fixed in the revision; what stays as record:

52. **"After" is by arrival, everywhere, including the floors.** The
    consent floor (#33) compared `ts >= moved_at`; an approval that shared
    the reversal's second but arrived before it revived consent, and both
    the rolled-back F418 and the aborted A232 read `ready`. The audit's
    as-of floor had the mirror hole. Every "latest" view here already
    dedups by `seq` (#12); the new floors now do too (`moved_seq`,
    `started_seq`, and — after the pass, #58 — the plan, verification and
    break-glass floors), and direction, reversal and membership order
    freights by `(discovered_at, seq)`. The lesson is #12 restated for relations:
    any comparison between two facts is by arrival, and `ts` is for
    display and for the clock-relative terms only.

53. **A reversal is current only until intent returns.** `v_reversal`
    held for F418 after a mutation re-decided it, so lanes said rolled
    back while the grid — which derived the same thing inline — said
    pending. The view now requires that no later decision returned to the
    freight, and the grid reads it too; #39's "one derivation" is true
    now, not merely claimed.

54. **Per-stack is the grain of "live", as it was of "carried" (#4, #30).**
    On platform/prod between legs `v_carried` is NULL, so the first
    `v_live` emitted nothing and a read of the true per-stack state was
    drift on every service. `v_live` is per stack now: a stable row per
    distinct carried-stack freight, a canary row per distinct open-leg
    freight. `previously_carried` had the mirror defect (satisfied by a
    freight one stack ran) and now requires every stack. The stack keeps
    not being a subject (#30) and keeps costing a view each time.

55. **An enactment's claim about its step is unchecked, and a
    re-declaration re-interprets it.** A phase citing a step past the
    declared steps silently dropped the step gate (now a lint error); a
    stage re-declared mid-rollout evaluates the executor's step index
    against the new declaration, since the transition does not cite the
    declaration it started under. Same family as #31: the transition
    should ref the `stage.declared` it enacts. Recorded, not fixed.

56. **The rollback block is whatever the team declares.** payments/prod's
    (`auto`, `previously_carried`) admits a jump to any freight prod ever
    ran and ignores a hold, because that is all it says; the fixture's
    story is the abort, and the block is minimal for it. The refuter
    downgraded the finding to "as declared". The real point for OPEN 4:
    a rollback block with no `not_held` is probably a lint warning, not a
    choice.

57. **Prose composed in SQL cannot be reformatted.** The lapsed-approval
    reason carries raw ISO timestamps into the page because the sentence
    is built in `v_gate_term`; the renderer formats every other time.
    The honest split is structured columns (which approval, which
    decision) and a sentence built in the renderer — deferred, since the
    checks pin the sentence.

## 2026-09-01 — pass 4's two dropped findings, recovered

Pass 4 dropped two findings whose refuters produced no structured output.
The following session pulled them from the workflow journal:

58. **The plan floor was the third clock site, and #52 was one floor short
    until it moved.** `v_plan.current` asked whether another freight had
    been carried *after* the plan by `finished_at > ts`, and the audit's
    as-of test asked whether the plan was on record *before* the decision
    by `ts <= ts`. A safe plan sharing a carry's second but arriving before
    it read current, and production-eu's auto-if-safe follower would have
    followed a rollback on it; the audit credited a plan arriving after the
    decision in its own second. Latent in both fixtures (no plan/carry
    tie), which is also why the pass's own refuter could say the earlier
    fixes "covered the ties" — it enumerated the ties it knew. All four
    comparisons are by `seq` now; two mutations pin the two sites
    separately, because the audit re-derives currency inline rather than
    reading `v_plan` (it needs as-of-decision semantics), and a fix to one
    site says nothing about the other — the mirror-hole pattern of #52
    again. Since one more instance had survived a pass aimed at the
    class, the rest of the file was swept for comparisons between two
    facts' timestamps: the verification floor (`v.ts >= first carry`),
    the break-glass join (`bg.ts > carried since`) and the uptake audit's
    as-of (`a.ts <= d.ts`, `p.ts <= d.ts`) were the same shape and are
    by `seq` too (a third mutation pins the verification floor); the
    harnesses' decision replay, which sliced the ledger by the decision's
    *timestamp*, now cuts it at the decision's position. What remains on
    `ts` is legitimately clock: `as_of`, `within_hours`, and release-card
    membership (a PR's merge time against a cut — the world's order, not
    the ledger's). The standing suggestion in threads (state "compare
    facts by seq" at the top of views.sql and lint for it) is still the
    remedy for the class; a sweep is a one-time substitute for it.

59. **Desired-relative direction reclassifies the incumbent.** Once a
    newer freight is decided for a stage, the freight it still *carries*
    is older than desired, so a move back to it is a rollback, and the
    approval that admitted it has lapsed (#33). That is the same relation
    that makes an abort a reversal (#47), read from the other end: the
    incumbent's gate is whatever a rollback *to* it would need to pass,
    which is right when the newer freight has started and arguable when
    it has not. Recorded, not changed; it is one of the direction calls
    the sketches made unilaterally.
