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
   demand.

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
    have written it (actor is the stage policy; required approval on
    record at decision time; evidence cited). Two mutation checks pin it,
    and the fixture-level check that every policy decision passed its gate
    at its own timestamp is the same idea run against history.

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
