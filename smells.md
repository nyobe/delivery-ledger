# Design smells

The acceptance test from the sketch: *if a view needs its own mutable state,
that's a design smell worth recording.* Everything here was noticed while
writing `views.sql` and `render.py` against the fixture. Dated entries; a
smell that gets resolved says how.

## 2026-09-01 — first slice

**No view needed a second mutable table.** Every screen — grid, lanes,
awaiting, trace, diff-gate, pending uptake, drift, releases, transition
detail — is a SELECT over `facts`. Recorded as the positive result the slice
was built to test.

The near-misses, honestly:

1. **`clock`** — a one-row table holding "now". Holds have an expiry and
   "active" is relative; the renderer sets the clock. This is the one piece
   of non-ledger state and it is an *input*, not stored status. Letting the
   caller set it is what makes time-travel rendering possible. Keep.

2. **Gate rules hand-compiled to SQL.** `policy.declared` carries the rule
   as text (`verified(staging, F, 'integration-tests') AND …`) and
   `v_gate_eval` evaluates it as a per-stage `CASE`. The text is documentation
   of what the SQL does; nothing reads it. An expression evaluator over the
   fact schema (OPEN 4 in the architecture — expr-lang / CEL / a subset)
   would generate `v_gate_all`'s columns from the rule. Until then, adding a
   stage means editing the view. Smell in the code, not in the ledger.

3. **The warehouse is a pseudo-stage.** `v_candidate` special-cases upstream
   values `warehouse:master` and `release-train`. The release train in
   particular is a real subject in the pipeline (Keith's cards are keyed on
   it) that this slice models only as a `release.cut` fact on the freight.
   If the train grows behaviour — a train carries several freights over its
   life, or is closed without shipping — it wants to be a subject.

4. **`v_carried` privileges `stack = 'service'`.** The grid is per stage, but
   production hosts three stacks (service, workflow-pool, workflow-ami). The
   service stack is what the release train moves, so the grid follows it;
   the AMI edge renders separately. A stage-with-many-stacks grid needs
   (stage, stack) cells and a per-stack notion of "carried". Deferred until a
   scenario demands it.

5. **`state.observed` reports freight ids.** The conformance watch says
   `api: F416`; in reality it would read ECS task-definition SHA tags (what
   `prod-rollback` matches on) and *map* them to freight through the
   freight's source SHA. That mapping is a hidden join done in the watch. The
   honest version is a content key on the deployed artifact — the OCI digest
   in the task definition — so the watch reports what it saw and the ledger
   does the join. Same gap the architecture names for ESC (no lock file, no
   captured component versions).

6. **`id` and `refs` are columns, not payload.** The sketch said payload
   until a view demands structure. Facts citing facts (`refs`) is used by
   every promotion decision and every approval, and the rendered page links
   fact ids everywhere — so they were promoted on day one rather than after a
   JOIN became unreadable. Noting the deviation; it earned its place
   immediately.

7. **Approval scope.** `v_approval` matches on (stage, freight). An approval
   granted on production does not count for production-eu (a self-check pins
   this). Whether an approval should be able to cover a *composite* — "ship
   F418 to US and EU" as one decision — is the composite-freight question from
   the architecture; this slice keeps approvals per subject.

8. **`awaiting_since`** is the latest satisfied prerequisite's time (or the
   candidate's availability). It is derived, but the derivation is a `max()`
   over whichever prerequisite columns exist — it is right for these rules and
   would need generating along with (2) for arbitrary ones.
