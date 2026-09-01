-- Views over the ledger. Every screen the renderer shows is one of these.
--
-- Layering: subjects (latest declaration wins, by arrival order) → per-subject
-- current state (latest fact of a kind) → derived states (candidate, gate
-- terms, drift) → screens (grid, lanes, trace, uptake, releases, audit).
-- Nothing here is stored; delete the facts and every view is empty.
--
-- "Latest" is always by `seq` (arrival), never by `ts` alone: two facts can
-- share a timestamp, and the ledger's order is the tie-break.

---------------------------------------------------------------------------
-- Subjects: latest declaration wins
---------------------------------------------------------------------------

CREATE VIEW v_warehouse AS
SELECT substr(f.subject, 11)                        AS name,
       json_extract(f.payload, '$.repo')            AS repo,
       json_extract(f.payload, '$.branch')          AS branch,
       json_extract(f.payload, '$.images')          AS images,
       json_extract(f.payload, '$.program')         AS program,
       json_extract(f.payload, '$.stacks')          AS stacks,
       f.ts AS declared_at, f.id AS fact
FROM facts f
WHERE f.kind = 'warehouse.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- The stacks a program's freight enacts (declared on its warehouse). A stage
-- may host other stacks too; those carry records, not freight.
CREATE VIEW v_program_stack AS
SELECT w.program, je.value AS stack
FROM v_warehouse w, json_each(w.stacks) je;

CREATE VIEW v_stage AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.order')           AS ord,
       json_extract(f.payload, '$.display')         AS display,
       json_extract(f.payload, '$.program')         AS program,
       json_extract(f.payload, '$.environment')     AS environment,
       json_extract(f.payload, '$.region')          AS region,
       json_extract(f.payload, '$.url')             AS url,
       json_extract(f.payload, '$.ops')             AS ops,
       json_extract(f.payload, '$.owner')           AS owner,
       json_extract(f.payload, '$.slack')           AS slack,
       json_extract(f.payload, '$.upstream')        AS upstream,
       json_extract(f.payload, '$.stacks')          AS stacks,
       f.ts AS declared_at, f.actor AS declared_by, f.id AS fact
FROM facts f
WHERE f.kind = 'stage.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

CREATE VIEW v_policy AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.mode')            AS mode,
       json_extract(f.payload, '$.trigger')         AS trigger,
       json_extract(f.payload, '$.rule')            AS rule,
       json_extract(f.payload, '$.terms')           AS terms,
       json_extract(f.payload, '$.safe')            AS safe_rule,
       json_extract(f.payload, '$.description')     AS description,
       -- the rollback direction, when the policy declares one (a stage without
       -- a rollback block gates a rollback with its ordinary terms)
       json_extract(f.payload, '$.rollback.mode')   AS rollback_mode,
       json_extract(f.payload, '$.rollback.terms')  AS rollback_terms,
       json_extract(f.payload, '$.rollback.rule')   AS rollback_rule,
       json_extract(f.payload, '$.rollback.description') AS rollback_description,
       f.ts AS declared_at, f.id AS fact
FROM facts f
WHERE f.kind = 'policy.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- One row per gate term per direction, in the order the policy lists them.
-- The term types are the vocabulary a gate-expression language would need
-- (OPEN 4). A promotion to a freight older than what the stage carries is
-- evaluated against the rollback terms; everything else against the forward
-- ones.
CREATE VIEW v_policy_term AS
SELECT p.stage, 'forward'                           AS direction,
       t.key                                        AS idx,
       json_extract(t.value, '$.type')              AS type,
       json_extract(t.value, '$.stage')             AS term_stage,
       json_extract(t.value, '$.check')             AS chk,
       json_extract(t.value, '$.role')              AS role,
       json_extract(t.value, '$.via')               AS via,
       json_extract(t.value, '$.within_hours')      AS within_hours
FROM v_policy p, json_each(p.terms) t
UNION ALL
SELECT p.stage, 'rollback',
       t.key,
       json_extract(t.value, '$.type'),
       json_extract(t.value, '$.stage'),
       json_extract(t.value, '$.check'),
       json_extract(t.value, '$.role'),
       json_extract(t.value, '$.via'),
       json_extract(t.value, '$.within_hours')
FROM v_policy p, json_each(coalesce(p.rollback_terms, p.terms)) t;

-- A binding is consumer-resident wiring (P4): by-reference (a per-stage
-- record, taken up under the edge's own policy) or by-version (a pin in the
-- consumer's config, riding its freight). Declared once per program as a
-- pattern, instantiated per environment; `pattern` cites the declaration.
CREATE VIEW v_edge AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.consumer')        AS consumer,
       json_extract(f.payload, '$.producer')        AS producer,
       json_extract(f.payload, '$.key')             AS key,
       coalesce(json_extract(f.payload, '$.kind'), 'by-reference') AS kind,
       json_extract(f.payload, '$.uptake')          AS uptake,
       json_extract(f.payload, '$.terms')           AS terms,
       json_extract(f.payload, '$.environment')     AS environment,
       json_extract(f.payload, '$.consumer_program') AS consumer_program,
       json_extract(f.payload, '$.producer_program') AS producer_program,
       json_extract(f.payload, '$.pattern')         AS pattern,
       coalesce(json_extract(f.payload, '$.role'), 'instance') AS role,
       json_extract(f.payload, '$.rule')            AS rule,
       json_extract(f.payload, '$.safe')            AS safe_rule,
       json_extract(f.payload, '$.environments')    AS environments,
       json_extract(f.payload, '$.description')     AS description,
       f.id AS fact
FROM facts f
WHERE f.kind = 'binding.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Freight is discovered (observation) and may later be cut as a release (intent).
CREATE VIEW v_freight AS
SELECT substr(f.subject, 9)                         AS freight,
       json_extract(f.payload, '$.warehouse')       AS warehouse,
       json_extract(f.payload, '$.source.sha')      AS sha,
       json_extract(f.payload, '$.source.branch')   AS branch,
       json_extract(f.payload, '$.build')           AS build,
       json_extract(f.payload, '$.prs')             AS prs,
       json_extract(f.payload, '$.images')          AS images,
       json_extract(f.payload, '$.config')          AS config,
       f.ts AS discovered_at, f.seq AS seq, f.id AS fact,
       c.ts                                         AS cut_at,
       json_extract(c.payload, '$.release_pr')      AS release_pr,
       json_extract(c.payload, '$.branch')          AS release_branch,
       json_extract(c.payload, '$.title')           AS release_title,
       c.id                                         AS cut_fact
FROM facts f
LEFT JOIN facts c ON c.kind = 'release.cut' AND c.subject = f.subject
                 AND c.seq = (SELECT max(x.seq) FROM facts x WHERE x.kind = 'release.cut' AND x.subject = f.subject)
WHERE f.kind = 'freight.discovered';

-- Membership is cumulative along a warehouse's branch: a PR merged to master
-- is in every later master build. `prs` on a freight lists what that build
-- introduced. Keyed by warehouse — two programs' branches are two histories.
CREATE VIEW v_membership AS
SELECT later.freight, later.warehouse,
       json_extract(pr.value, '$.number')           AS pr,
       json_extract(pr.value, '$.title')            AS title,
       json_extract(pr.value, '$.author')           AS author,
       earlier.freight                              AS introduced_in,
       earlier.discovered_at                        AS merged_at
FROM v_freight later
JOIN v_freight earlier ON earlier.warehouse = later.warehouse
                      AND (earlier.discovered_at < later.discovered_at OR earlier.freight = later.freight),
     json_each(earlier.prs) pr;

-- What a release train ships: PRs introduced since the previous cut (Keith's
-- release card membership).
CREATE VIEW v_release_prs AS
SELECT r.freight, m.pr, m.title, m.author, m.introduced_in
FROM v_freight r
JOIN v_membership m ON m.freight = r.freight
WHERE r.cut_at IS NOT NULL
  AND m.merged_at > coalesce((SELECT max(p.discovered_at) FROM v_freight p
                              WHERE p.warehouse = r.warehouse AND p.cut_at IS NOT NULL AND p.cut_at < r.cut_at), '');

---------------------------------------------------------------------------
-- Per-subject current state: latest fact of a kind
---------------------------------------------------------------------------

-- Intent: what each stage should carry (latest decision wins).
CREATE VIEW v_desired AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       f.ts AS decided_at, f.actor AS decided_by, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'promotion.decided'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Every promotion decision, with the freight the stage was decided to before it.
CREATE VIEW v_decision AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       f.ts, f.seq, f.actor, f.rationale, f.id AS fact,
       (SELECT json_extract(p.payload, '$.freight') FROM facts p
         WHERE p.kind = 'promotion.decided' AND p.subject = f.subject AND p.seq < f.seq
         ORDER BY p.seq DESC LIMIT 1)                AS from_freight
FROM facts f
WHERE f.kind = 'promotion.decided';

-- When intent last moved away from a freight at a stage: the latest decision
-- there for another freight, after this one was first decided. Consent lapses
-- when intent moves on — an approval only counts once it postdates that
-- instant — so the standing approval that shipped a freight cannot re-promote
-- it after a rollback, a rollback target's old approval cannot wave the
-- rollback through, and an aborted canary's approval does not re-fire it.
-- NULL while intent has never moved away.
CREATE VIEW v_intent_moved AS
SELECT d.stage, d.freight, d.first_seq,
       (SELECT max(o.ts) FROM v_decision o
         WHERE o.stage = d.stage AND o.freight <> d.freight AND o.seq > d.first_seq) AS moved_at,
       (SELECT o.freight FROM v_decision o
         WHERE o.stage = d.stage AND o.freight <> d.freight AND o.seq > d.first_seq
         ORDER BY o.seq DESC LIMIT 1)                AS moved_to
FROM (SELECT stage, freight, min(seq) AS first_seq FROM v_decision GROUP BY stage, freight) d;

-- A reversal: intent moved from a freight to an older one. Derived from two
-- decisions, never written; lanes, the grid and the trace all read this one
-- view for "rolled back".
CREATE VIEW v_reversal AS
SELECT m.stage, m.freight, m.moved_at AS at, m.moved_to AS to_freight
FROM v_intent_moved m
JOIN v_freight f  ON f.freight = m.freight
JOIN v_freight t  ON t.freight = m.moved_to
WHERE t.discovered_at < f.discovered_at;

-- A transition is the enactment: started once, phases and steps in between,
-- finished at most once (succeeded | failed | abandoned).
CREATE VIEW v_transition AS
SELECT s.subject                                    AS transition,
       json_extract(s.payload, '$.stage')           AS stage,
       json_extract(s.payload, '$.freight')         AS freight,
       json_extract(s.payload, '$.stack')           AS stack,
       json_extract(s.payload, '$.record_version')  AS record_version,
       json_extract(s.payload, '$.run')             AS run,
       json_extract(s.payload, '$.strategy')        AS strategy,
       s.ts AS started_at, s.seq AS started_seq,
       e.ts AS finished_at, e.seq AS finished_seq,
       json_extract(e.payload, '$.outcome')         AS outcome,
       json_extract(e.payload, '$.ops_update')      AS ops_update,
       json_extract(e.payload, '$.summary')         AS summary,
       json_extract(e.payload, '$.error')           AS error,
       json_extract(e.payload, '$.failed_step')     AS failed_step,
       json_extract(e.payload, '$.step_url')        AS step_url,
       json_extract(e.payload, '$.detail')          AS detail,
       (SELECT json_extract(p.payload, '$.phase') FROM facts p
         WHERE p.kind = 'transition.phase' AND p.subject = s.subject
         ORDER BY p.seq DESC LIMIT 1)                AS last_phase,
       (SELECT count(*) FROM facts r
         WHERE r.kind = 'resource.step' AND r.subject = s.subject) AS resource_steps,
       s.id AS started_fact, e.id AS finished_fact
FROM facts s
LEFT JOIN facts e ON e.kind = 'transition.finished' AND e.subject = s.subject
                 AND e.seq = (SELECT max(x.seq) FROM facts x WHERE x.kind = 'transition.finished' AND x.subject = s.subject)
WHERE s.kind = 'transition.started';

-- Transitions on a stage's freight stacks (the stacks its program's freight
-- enacts). Enactments of other stacks hosted there — record uptakes on a
-- worker pool — are transitions too, but they do not move the freight lane.
CREATE VIEW v_freight_transition AS
SELECT t.*
FROM v_transition t
JOIN v_stage s ON s.stage = t.stage
JOIN v_program_stack ps ON ps.program = s.program AND ps.stack = t.stack;

-- Observation: what each (stage, stack) actually carries — the latest
-- freight enactment on that stack that finished successfully.
CREATE VIEW v_carried_stack AS
SELECT t.stage, t.stack, t.freight,
       -- since: the first success of this freight after the last success of any other
       -- (an uptake re-enacting the same freight does not restart the clock)
       (SELECT min(u.finished_at) FROM v_freight_transition u
         WHERE u.stage = t.stage AND u.stack = t.stack AND u.outcome = 'succeeded' AND u.freight = t.freight
           AND u.finished_seq > coalesce((SELECT max(v.finished_seq) FROM v_freight_transition v
                                          WHERE v.stage = t.stage AND v.stack = t.stack AND v.outcome = 'succeeded'
                                            AND v.freight IS NOT NULL AND v.freight <> t.freight), 0)) AS since,
       t.finished_at AS last_enacted_at, t.finished_seq, t.ops_update, t.transition, t.finished_fact AS fact
FROM v_freight_transition t
WHERE t.outcome = 'succeeded' AND t.freight IS NOT NULL
  AND t.finished_seq = (SELECT max(u.finished_seq) FROM v_freight_transition u
                        WHERE u.stage = t.stage AND u.stack = t.stack
                          AND u.outcome = 'succeeded' AND u.freight IS NOT NULL);

-- When a stage first carried a freight on every one of its freight stacks.
CREATE VIEW v_first_carried AS
SELECT s.stage, t.freight, max(t.first_at) AS at
FROM v_stage s
JOIN (SELECT x.stage, x.stack, x.freight, min(x.finished_at) AS first_at
        FROM v_freight_transition x
       WHERE x.outcome = 'succeeded' AND x.freight IS NOT NULL
       GROUP BY x.stage, x.stack, x.freight) t ON t.stage = s.stage
GROUP BY s.stage, t.freight
HAVING count(*) = (SELECT count(*) FROM v_program_stack ps WHERE ps.program = s.program);

-- What a stage carries: the freight every one of its freight stacks carries,
-- or NULL with the per-stack detail when they disagree (a partial rollout).
-- `since` and the update handle come from the stack that finished last.
CREATE VIEW v_carried AS
SELECT s.stage,
       CASE WHEN count(k.stack) = n.n AND count(DISTINCT k.freight) = 1 THEN min(k.freight) END AS freight,
       max(k.since)                                 AS since,
       last.ops_update, last.transition, last.fact,
       count(k.stack)                               AS n_stacks_carrying,
       n.n                                          AS n_stacks,
       (SELECT group_concat(x, ', ') FROM (SELECT k2.stack || '@' || k2.freight AS x FROM v_carried_stack k2
                                            WHERE k2.stage = s.stage ORDER BY k2.stack)) AS stacks_detail
FROM v_stage s
JOIN (SELECT program, count(*) AS n FROM v_program_stack GROUP BY program) n ON n.program = s.program
JOIN v_carried_stack k ON k.stage = s.stage
JOIN v_carried_stack last ON last.stage = s.stage
     AND last.transition = (SELECT k3.transition FROM v_carried_stack k3 WHERE k3.stage = s.stage
                            ORDER BY k3.since DESC, k3.finished_seq DESC LIMIT 1)
GROUP BY s.stage;

-- One row per stage (the grid joins on it): the latest open leg is the
-- representative; every open leg is counted and named, since a multi-stack
-- promotion can have several in flight at once.
CREATE VIEW v_inflight AS
SELECT t.*,
       (SELECT count(*) FROM v_freight_transition u WHERE u.stage = t.stage AND u.finished_at IS NULL) AS n_inflight,
       (SELECT group_concat(x, ', ') FROM (SELECT u.stack || '@' || coalesce(u.freight, 'record v' || u.record_version) AS x
                                            FROM v_freight_transition u WHERE u.stage = t.stage AND u.finished_at IS NULL
                                            ORDER BY u.stack)) AS inflight_detail
FROM v_freight_transition t
WHERE t.finished_at IS NULL
  AND t.started_seq = (SELECT max(u.started_seq) FROM v_freight_transition u
                       WHERE u.stage = t.stage AND u.finished_at IS NULL);

CREATE VIEW v_last_finished AS
SELECT t.* FROM v_freight_transition t
WHERE t.finished_at IS NOT NULL
  AND t.finished_seq = (SELECT max(u.finished_seq) FROM v_freight_transition u
                        WHERE u.stage = t.stage AND u.finished_at IS NOT NULL);

-- The first time a stage carried a freight on any stack, and the last time it
-- did, by arrival order (a stage can carry a freight in several stints).
CREATE VIEW v_first_success AS
SELECT t.stage, t.freight, min(t.finished_seq) AS first_seq, min(t.finished_at) AS first_at,
       max(t.finished_seq) AS last_seq, max(t.finished_at) AS last_at
FROM v_freight_transition t
WHERE t.outcome = 'succeeded' AND t.freight IS NOT NULL
GROUP BY t.stage, t.freight;

-- The incumbent on a stage: what it carries, or — while its stacks disagree —
-- the latest freight that succeeded on any of them.
CREATE VIEW v_incumbent AS
SELECT s.stage,
       coalesce(k.freight, (SELECT t.freight FROM v_freight_transition t
                             WHERE t.stage = s.stage AND t.outcome = 'succeeded' AND t.freight IS NOT NULL
                             ORDER BY t.finished_seq DESC LIMIT 1)) AS freight
FROM v_stage s
LEFT JOIN v_carried k ON k.stage = s.stage;

-- Direction is derived, never written: promoting a freight discovered before
-- the incumbent is a rollback; anything else is forward. Rollback-ness is a
-- relation between two freights at a stage, not a property of a decision.
CREATE VIEW v_direction AS
SELECT s.stage, fr.freight,
       CASE WHEN inc.discovered_at IS NOT NULL AND fr.discovered_at < inc.discovered_at THEN 'rollback' ELSE 'forward' END AS direction,
       inc.freight AS incumbent
FROM v_stage s
CROSS JOIN v_freight fr
LEFT JOIN v_incumbent i ON i.stage = s.stage
LEFT JOIN v_freight inc ON inc.freight = i.freight;

-- Verification status per (stage, freight, check): latest outcome wins.
CREATE VIEW v_verified AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.check')           AS chk,
       json_extract(f.payload, '$.outcome')         AS outcome,
       json_extract(f.payload, '$.detail')          AS detail,
       f.actor, f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'verification.recorded'
  AND f.seq = (SELECT max(g.seq) FROM facts g
               WHERE g.kind = f.kind AND g.subject = f.subject
                 AND json_extract(g.payload, '$.freight') = json_extract(f.payload, '$.freight')
                 AND json_extract(g.payload, '$.check')   = json_extract(f.payload, '$.check'));

-- Every stage approval, with its role. Gates pick the latest matching one
-- that is still valid: given after intent last moved the stage away from the
-- freight (v_intent_moved).
CREATE VIEW v_approval AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.role')            AS role,
       json_extract(f.payload, '$.via')             AS via,
       f.actor, f.ts, f.seq, f.rationale, f.id AS fact,
       m.moved_at,
       (f.ts >= coalesce(m.moved_at, ''))           AS valid
FROM facts f
LEFT JOIN v_intent_moved m ON m.stage = substr(f.subject, 7) AND m.freight = json_extract(f.payload, '$.freight')
WHERE f.kind = 'approval.granted' AND f.subject LIKE 'stage:%';

-- An approval on an edge: for taking up one record version.
CREATE VIEW v_edge_approval AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.record_version')  AS version,
       json_extract(f.payload, '$.role')            AS role,
       json_extract(f.payload, '$.via')             AS via,
       f.actor, f.ts, f.seq, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'approval.granted' AND f.subject LIKE 'edge:%';

-- Holds are intent facts with an expiry; "active" is relative to the clock.
-- A stage may carry several; the gate sees one row per stage.
CREATE VIEW v_hold AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.until')           AS until_ts,
       json_extract(f.payload, '$.scope')           AS scope,
       f.actor, f.ts AS placed_at, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'hold.placed'
  AND json_extract(f.payload, '$.until') > (SELECT now FROM clock);

CREATE VIEW v_hold_active AS
SELECT stage,
       max(until_ts)                                AS until_ts,
       min(placed_at)                               AS placed_at,
       count(*)                                     AS n,
       group_concat(DISTINCT actor)                 AS holders,
       group_concat(rationale, ' | ')               AS rationale,
       group_concat(fact, ' ')                      AS facts
FROM v_hold
GROUP BY stage;

-- Plan facts are stage 3 of the key decomposition: (desired, world@T) → plan.
-- `safe` is the auto-if-safe policy's safe-rule; NULL when the plan lacks the
-- fields the rule reads (unknown is not safe).
CREATE VIEW v_plan AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.against')         AS against,
       json_extract(f.payload, '$.create')          AS n_create,
       json_extract(f.payload, '$.update')          AS n_update,
       json_extract(f.payload, '$.delete')          AS n_delete,
       json_extract(f.payload, '$.replace')         AS n_replace,
       json_extract(f.payload, '$.migrations_changed') AS migrations_changed,
       json_extract(f.payload, '$.migrations')      AS migrations,
       CASE WHEN json_extract(f.payload, '$.delete') IS NULL
              OR json_extract(f.payload, '$.replace') IS NULL
              OR json_extract(f.payload, '$.migrations_changed') IS NULL THEN NULL
            ELSE (json_extract(f.payload, '$.delete') = 0
                  AND json_extract(f.payload, '$.replace') = 0
                  AND NOT json_extract(f.payload, '$.migrations_changed')) END AS safe,
       -- a plan is against the world at T: once the stage has carried some
       -- other freight since it was computed, it is stale
       (NOT EXISTS (SELECT 1 FROM v_freight_transition o
                     WHERE o.stage = substr(f.subject, 7) AND o.outcome = 'succeeded' AND o.freight IS NOT NULL
                       AND o.freight <> json_extract(f.payload, '$.freight') AND o.finished_at > f.ts)) AS current,
       f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'plan.summarized'
  AND json_extract(f.payload, '$.against_record') IS NULL
  AND f.seq = (SELECT max(g.seq) FROM facts g
               WHERE g.kind = f.kind AND g.subject = f.subject
                 AND json_extract(g.payload, '$.against_record') IS NULL
                 AND json_extract(g.payload, '$.freight') = json_extract(f.payload, '$.freight'));

-- Conformance reads: what a watch last saw running, compared to intent.
CREATE VIEW v_observed AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.services')        AS services,
       json_extract(f.payload, '$.source')          AS source,
       f.ts, f.id AS fact,
       (SELECT count(*) FROM json_each(json_extract(f.payload, '$.services')) je
         WHERE je.value IS NOT (SELECT d.freight FROM v_desired d WHERE d.stage = substr(f.subject, 7)))
                                                    AS mismatches,
       (SELECT group_concat(je.key || '@' || je.value, ', ')
          FROM json_each(json_extract(f.payload, '$.services')) je
         WHERE je.value IS NOT (SELECT d.freight FROM v_desired d WHERE d.stage = substr(f.subject, 7)))
                                                    AS mismatch_detail
FROM facts f
WHERE f.kind = 'state.observed'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

CREATE VIEW v_observed_service AS
SELECT o.stage, je.key AS service, d.freight AS desired, je.value AS observed,
       (je.value IS d.freight) AS matches, o.ts, o.fact
FROM v_observed o, json_each(o.services) je
LEFT JOIN v_desired d ON d.stage = o.stage;

-- Break-glass is the side door: same class (intent), same attribution.
CREATE VIEW v_breakglass AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.scope')           AS scope,
       json_extract(f.payload, '$.action')          AS action,
       json_extract(f.payload, '$.from_freight')    AS from_freight,
       json_extract(f.payload, '$.to_freight')      AS to_freight,
       json_extract(f.payload, '$.incident')        AS incident,
       json_extract(f.payload, '$.expiry')          AS expiry,
       f.actor, f.ts, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'breakglass.recorded'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Side jobs (sentry markers, notifications): observations on the stage that
-- never gate anything; `optional` says whether a failure matters.
CREATE VIEW v_job AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.job')             AS job,
       json_extract(f.payload, '$.outcome')         AS outcome,
       json_extract(f.payload, '$.optional')        AS optional,
       json_extract(f.payload, '$.detail')          AS detail,
       f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'job.finished';

---------------------------------------------------------------------------
-- Derived: candidate, gate terms, gate
---------------------------------------------------------------------------

-- A rollback request nominates a freight the stage carried before, the way a
-- release cut nominates one for the train. It is open until the next decision
-- on the stage answers it.
CREATE VIEW v_rollback_request AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.incident')        AS incident,
       json_extract(f.payload, '$.via')             AS via,
       f.actor, f.ts, f.rationale, f.id AS fact, f.seq,
       (f.seq > coalesce((SELECT max(d.seq) FROM facts d WHERE d.kind = 'promotion.decided' AND d.subject = f.subject), 0)) AS open
FROM facts f
WHERE f.kind = 'rollback.requested'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- The candidate is what each stage's upstream offers it right now — or, while
-- a rollback request is open, the freight it asks for.
CREATE VIEW v_candidate AS
SELECT s.stage,
  CASE
    WHEN rr.open THEN rr.freight
    WHEN s.upstream LIKE 'warehouse:%' THEN
      (SELECT fr.freight FROM v_freight fr WHERE fr.warehouse = substr(s.upstream, 11)
        ORDER BY fr.discovered_at DESC, fr.seq DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.freight FROM v_freight fr JOIN v_warehouse w ON w.name = fr.warehouse AND w.program = s.program
        WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC, fr.seq DESC LIMIT 1)
    ELSE
      (SELECT k.freight FROM v_carried k WHERE k.stage = s.upstream)
  END AS freight,
  CASE
    WHEN rr.open THEN rr.ts
    WHEN s.upstream LIKE 'warehouse:%' THEN
      (SELECT fr.discovered_at FROM v_freight fr WHERE fr.warehouse = substr(s.upstream, 11)
        ORDER BY fr.discovered_at DESC, fr.seq DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.cut_at FROM v_freight fr JOIN v_warehouse w ON w.name = fr.warehouse AND w.program = s.program
        WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC, fr.seq DESC LIMIT 1)
    ELSE
      (SELECT k.since FROM v_carried k WHERE k.stage = s.upstream)
  END AS available_at,
  CASE WHEN rr.open THEN 'rollback request' ELSE 'upstream' END AS source,
  CASE WHEN rr.open THEN rr.actor END    AS requested_by,
  CASE WHEN rr.open THEN rr.incident END AS request_incident,
  CASE WHEN rr.open THEN rr.fact END     AS request_fact
FROM v_stage s
LEFT JOIN v_rollback_request rr ON rr.stage = s.stage;

-- Every gate term evaluated for every (stage, freight) pair, in the pair's
-- direction: satisfied_at is the time of the fact that satisfies it (NULL =
-- unmet). A verification only counts once the stage actually carried the
-- freight — you verify what ran. An approval only counts if given after
-- intent last moved the stage away from the freight — consent lapses when
-- intent moves on. A plan only counts if computed against what the stage
-- carries now — a plan is about the world at T. This is what makes "would
-- this gate pass right now?" answerable at rest.
CREATE VIEW v_gate_term AS
SELECT s.stage, fr.freight, dr.direction, t.idx, t.type, t.term_stage, t.chk, t.role, t.within_hours,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.ts FROM v_verified v
        WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk AND v.outcome = 'pass'
          AND v.ts >= coalesce((SELECT fc.at FROM v_first_carried fc
                                WHERE fc.stage = t.term_stage AND fc.freight = fr.freight), '9999'))
    WHEN 'carried' THEN
      (SELECT k.since FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.ts FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_hold_active h WHERE h.stage = s.stage) THEN '' END
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT pl.ts FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight AND pl.safe = 1 AND pl.current),
               (SELECT a.ts FROM v_approval a
                 WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid ORDER BY a.seq DESC LIMIT 1))
    WHEN 'previously_carried' THEN
      (SELECT fs.last_at FROM v_first_success fs
        WHERE fs.stage = s.stage AND fs.freight = fr.freight
          AND (t.within_hours IS NULL
               OR fs.last_at >= strftime('%Y-%m-%dT%H:%M:%SZ', (SELECT now FROM clock), '-' || t.within_hours || ' hours')))
  END AS satisfied_at,
  CASE t.type
    WHEN 'verified'              THEN 'verified in ' || t.term_stage || ': ' || t.chk
    WHEN 'carried'               THEN 'carried by ' || t.term_stage
    WHEN 'approved'              THEN 'approved by ' || t.role || coalesce(' (' || t.via || ')', '')
    WHEN 'not_held'              THEN 'no active hold'
    WHEN 'plan_safe_or_approved' THEN 'plan safe, or approved by ' || t.role
    WHEN 'previously_carried'    THEN 'carried by ' || s.stage || ' before' || coalesce(' (within ' || t.within_hours || 'h)', '')
  END AS label,
  CASE t.type
    WHEN 'verified' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_first_carried fc WHERE fc.stage = t.term_stage AND fc.freight = fr.freight)
           THEN 'verification: ' || t.chk || ' in ' || t.term_stage || ' (' || t.term_stage || ' has not carried ' || fr.freight || ')'
           WHEN EXISTS (SELECT 1 FROM v_verified v WHERE v.stage = t.term_stage AND v.freight = fr.freight
                          AND v.chk = t.chk AND v.outcome = 'pass'
                          AND v.ts < (SELECT fc.at FROM v_first_carried fc
                                      WHERE fc.stage = t.term_stage AND fc.freight = fr.freight))
           THEN 'verification: ' || t.chk || ' in ' || t.term_stage || ' (recorded before ' || t.term_stage || ' carried ' || fr.freight || ' — re-run)'
           ELSE 'verification: ' || t.chk || ' in ' || t.term_stage END
    WHEN 'carried'               THEN t.term_stage || ' to carry ' || fr.freight
    WHEN 'approved' THEN
      'approval: ' || t.role ||
      coalesce((SELECT ' (the approval at ' || a.ts || ' predates the decision that moved ' || s.stage || ' off ' || fr.freight || ' at ' || a.moved_at || ' — re-approve)'
                  FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND NOT a.valid
                 ORDER BY a.seq DESC LIMIT 1), '')
    WHEN 'not_held'              THEN 'hold until ' || (SELECT h.until_ts FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      CASE WHEN (SELECT pl.fact FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight) IS NULL
           THEN 'plan for ' || s.stage
           WHEN NOT (SELECT pl.current FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight)
           THEN 'plan for ' || s.stage || ' (the plan at ' || (SELECT pl.ts FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight)
                || ' predates what ' || s.stage || ' carries now — recompute)'
           ELSE 'approval: ' || t.role || ' (plan not safe)' ||
                coalesce((SELECT ' (the approval at ' || a.ts || ' predates the decision that moved ' || s.stage || ' off ' || fr.freight || ' — re-approve)'
                            FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND NOT a.valid
                           ORDER BY a.seq DESC LIMIT 1), '') END
    WHEN 'previously_carried' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_first_success fs WHERE fs.stage = s.stage AND fs.freight = fr.freight)
           THEN s.stage || ' never carried ' || fr.freight
           ELSE s.stage || ' last carried ' || fr.freight || ' at '
                || (SELECT fs.last_at FROM v_first_success fs WHERE fs.stage = s.stage AND fs.freight = fr.freight)
                || ', outside ' || t.within_hours || 'h' END
  END AS unmet_text,
  -- when the wait on an unmet term began, where the term has an onset of its
  -- own: a hold's placement, a plan's timestamp, or — for an approval that
  -- lapsed — the decision that moved intent away
  CASE t.type
    WHEN 'not_held'              THEN (SELECT h.placed_at FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'approved'              THEN (SELECT m.moved_at FROM v_intent_moved m WHERE m.stage = s.stage AND m.freight = fr.freight
                                        AND EXISTS (SELECT 1 FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND NOT a.valid))
    WHEN 'plan_safe_or_approved' THEN max(coalesce((SELECT pl.ts FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight AND pl.current), ''),
                                         coalesce((SELECT m.moved_at FROM v_intent_moved m WHERE m.stage = s.stage AND m.freight = fr.freight
                                                    AND EXISTS (SELECT 1 FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND NOT a.valid)), ''))
  END AS onset_at,
  -- how a disjunctive term was (or wasn't) met — read by the diff-gate screen
  CASE t.type
    WHEN 'plan_safe_or_approved' THEN
      CASE WHEN EXISTS (SELECT 1 FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight AND pl.safe = 1 AND pl.current) THEN 'auto'
           WHEN EXISTS (SELECT 1 FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid) THEN 'approved'
           WHEN NOT EXISTS (SELECT 1 FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight) THEN 'no-plan'
           WHEN NOT EXISTS (SELECT 1 FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight AND pl.current) THEN 'stale'
           ELSE 'open' END
  END AS term_outcome,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.fact FROM v_verified v WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk)
    WHEN 'carried' THEN
      (SELECT k.fact FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.fact FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.facts FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT a.fact FROM v_approval a
                 WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid ORDER BY a.seq DESC LIMIT 1),
               (SELECT pl.fact FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight))
    WHEN 'previously_carried' THEN
      (SELECT t2.finished_fact FROM v_freight_transition t2
        WHERE t2.stage = s.stage AND t2.freight = fr.freight AND t2.outcome = 'succeeded' ORDER BY t2.finished_seq DESC LIMIT 1)
  END AS evidence_fact,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.outcome || ': ' || v.detail FROM v_verified v
        WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk)
    WHEN 'carried' THEN
      (SELECT 'since ' || k.since FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.actor || ' via ' || a.via FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role AND a.valid ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.holders || ': ' || h.rationale FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      (SELECT 'plan +' || pl.n_create || ' ~' || pl.n_update || ' −' || pl.n_delete || ' ±' || pl.n_replace
              || CASE WHEN pl.migrations_changed THEN ', migrations' ELSE '' END
              || CASE WHEN pl.safe = 1 THEN ' — safe' ELSE ' — not safe' END
              || CASE WHEN pl.current THEN '' ELSE ' (stale)' END
         FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight)
    WHEN 'previously_carried' THEN
      (SELECT 'first ' || fs.first_at || ', last ' || fs.last_at FROM v_first_success fs WHERE fs.stage = s.stage AND fs.freight = fr.freight)
  END AS evidence
FROM v_stage s
CROSS JOIN v_freight fr
JOIN v_direction dr ON dr.stage = s.stage AND dr.freight = fr.freight
JOIN v_policy_term t ON t.stage = s.stage AND t.direction = dr.direction;

-- The gate for every (stage, freight): passes iff no term is unmet; awaiting
-- is the first unmet term; gate_since is when the wait on it began. The rule
-- and mode are the ones for the pair's direction.
CREATE VIEW v_gate AS
SELECT s.stage, fr.freight, dr.direction,
  CASE WHEN dr.direction = 'rollback' THEN coalesce(p.rollback_mode, p.mode) ELSE p.mode END AS mode,
  CASE WHEN dr.direction = 'rollback' THEN coalesce(p.rollback_rule, p.rule) ELSE p.rule END AS rule,
  (SELECT count(*) FROM v_gate_term g WHERE g.stage = s.stage AND g.freight = fr.freight) AS n_terms,
  (SELECT count(*) FROM v_gate_term g WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NULL) AS n_unmet,
  ((SELECT count(*) FROM v_gate_term g WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NULL) = 0) AS passes,
  (SELECT g.unmet_text FROM v_gate_term g WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NULL
     ORDER BY g.idx LIMIT 1) AS awaiting,
  (SELECT g.type FROM v_gate_term g WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NULL
     ORDER BY g.idx LIMIT 1) AS awaiting_type,
  max(coalesce((SELECT max(g.satisfied_at) FROM v_gate_term g
                 WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NOT NULL), ''),
      coalesce((SELECT g.onset_at FROM v_gate_term g
                 WHERE g.stage = s.stage AND g.freight = fr.freight AND g.satisfied_at IS NULL
                 ORDER BY g.idx LIMIT 1), '')) AS gate_since
FROM v_stage s
CROSS JOIN v_freight fr
JOIN v_direction dr ON dr.stage = s.stage AND dr.freight = fr.freight
JOIN v_policy p ON p.stage = s.stage;

-- The gate as it applies to each stage's candidate.
CREATE VIEW v_gate_eval AS
SELECT g.*, c.available_at, c.source, c.requested_by, c.request_incident, c.request_fact,
       max(coalesce(c.available_at, ''), coalesce(g.gate_since, '')) AS awaiting_since
FROM v_gate g
JOIN v_candidate c ON c.stage = g.stage AND c.freight = g.freight;

---------------------------------------------------------------------------
-- Screens
---------------------------------------------------------------------------

-- The subject grid: one row per stage — desired, carried, and the derived
-- state. No status is stored anywhere; every column is a join. Comparisons
-- use IS / IS NOT so a stage with no decision yet still gets a state.
CREATE VIEW v_grid AS
SELECT s.ord, s.stage, s.program, s.environment, s.region, s.owner, s.url, s.ops,
       d.freight AS desired, d.decided_at, d.decided_by,
       k.freight AS carried, k.since AS carried_since, k.ops_update,
       k.n_stacks, k.n_stacks_carrying, k.stacks_detail,
       i.transition AS inflight, i.freight AS inflight_freight, i.last_phase, i.started_at AS inflight_since,
       i.n_inflight, i.inflight_detail,
       lf.outcome AS last_outcome, lf.freight AS last_outcome_freight, lf.finished_at AS last_outcome_at,
       lf.error AS last_error, lf.transition AS last_transition, lf.failed_step, lf.step_url,
       ge.freight AS candidate, ge.passes, ge.awaiting, ge.awaiting_type, ge.awaiting_since,
       ge.direction AS candidate_direction, ge.source AS candidate_source,
       ge.requested_by, ge.request_incident, ge.request_fact,
       -- a reversal: the latest decision moved intent to a freight older than the one before it
       CASE WHEN pf.discovered_at > df.discovered_at THEN dd.from_freight END AS rolled_back_from,
       CASE WHEN pf.discovered_at > df.discovered_at THEN d.decided_at END    AS rolled_back_at,
       -- what the engine last enacted here (the latest success that ran a Pulumi update)
       eng.freight AS engine_freight, eng.ops_update AS engine_update,
       CASE
         WHEN i.transition IS NOT NULL AND i.freight IS NOT NULL
              AND i.freight IS NOT d.freight                                        THEN 'superseded'
         WHEN i.transition IS NOT NULL                                              THEN 'in-flight'
         WHEN lf.outcome = 'failed' AND lf.freight IS d.freight
              AND d.freight IS NOT k.freight                                        THEN 'failed'
         WHEN k.stacks_detail IS NOT NULL AND k.freight IS NULL                     THEN 'partial'
         WHEN ge.freight IS NOT NULL AND ge.freight IS NOT d.freight
              AND ge.awaiting_type = 'not_held'                                     THEN 'held'
         WHEN ge.freight IS NOT NULL AND ge.freight IS NOT d.freight AND ge.passes = 0 THEN 'awaiting'
         WHEN ge.freight IS NOT NULL AND ge.freight IS NOT d.freight AND ge.passes = 1 THEN 'ready'
         WHEN d.freight IS NOT NULL AND d.freight IS k.freight                      THEN 'converged'
         WHEN d.freight IS NOT NULL                                                 THEN 'pending'
         ELSE 'idle'
       END AS status,
       CASE WHEN o.mismatches > 0 THEN
         o.mismatch_detail ||
         CASE WHEN bg.fact IS NOT NULL
              THEN ' — break-glass ' || bg.incident || ' (' || bg.fact || ')'
              ELSE ' — UNEXPLAINED' END
       END AS drift,
       o.ts AS observed_at, o.fact AS observed_fact,
       h.until_ts AS hold_until, h.holders AS hold_by, h.rationale AS hold_rationale, h.n AS holds
FROM v_stage s
LEFT JOIN v_desired       d  ON d.stage  = s.stage
LEFT JOIN v_decision      dd ON dd.fact  = d.fact
LEFT JOIN v_freight       df ON df.freight = d.freight
LEFT JOIN v_freight       pf ON pf.freight = dd.from_freight
LEFT JOIN v_carried       k  ON k.stage  = s.stage
LEFT JOIN v_inflight      i  ON i.stage  = s.stage
LEFT JOIN v_last_finished lf ON lf.stage = s.stage
LEFT JOIN v_gate_eval     ge ON ge.stage = s.stage
LEFT JOIN v_observed      o  ON o.stage  = s.stage
LEFT JOIN v_breakglass    bg ON bg.stage = s.stage AND bg.ts > coalesce(k.since, '')
LEFT JOIN v_hold_active   h  ON h.stage  = s.stage
LEFT JOIN v_freight_transition eng ON eng.stage = s.stage AND eng.outcome = 'succeeded' AND eng.ops_update IS NOT NULL
     AND eng.finished_seq = (SELECT max(x.finished_seq) FROM v_freight_transition x
                             WHERE x.stage = s.stage AND x.outcome = 'succeeded' AND x.ops_update IS NOT NULL AND x.freight IS NOT NULL)
ORDER BY s.ord;

-- Freight lanes: freight × stage, long form. The renderer pivots it and
-- switches on `cell`; it never re-derives the classification.
CREATE VIEW v_lanes_base AS
SELECT fr.freight, fr.release_pr, fr.cut_at, fr.discovered_at, s.stage, s.ord,
  (SELECT fc.at FROM v_first_carried fc WHERE fc.stage = s.stage AND fc.freight = fr.freight) AS reached_at,
  (SELECT t.started_at FROM v_freight_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.finished_at IS NULL
    ORDER BY t.started_seq DESC LIMIT 1) AS inflight_since,
  (SELECT t.outcome FROM v_freight_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.finished_at IS NOT NULL
    ORDER BY t.finished_seq DESC LIMIT 1) AS last_outcome,
  (SELECT t.finished_at FROM v_freight_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.finished_at IS NOT NULL
    ORDER BY t.finished_seq DESC LIMIT 1) AS last_outcome_at,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0 AND d.freight IS NOT fr.freight
       THEN ge.awaiting END AS awaiting,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0 AND d.freight IS NOT fr.freight
       THEN ge.awaiting_since END AS awaiting_since,
  (k.freight IS fr.freight) AS is_current,
  CASE WHEN k.freight IS fr.freight THEN k.since END AS current_since,
  rv.at AS rolled_back_at, rv.to_freight AS rolled_back_to,
  (SELECT count(*) FROM v_carried_stack k2 WHERE k2.stage = s.stage AND k2.freight = fr.freight) AS n_stacks_at,
  (SELECT group_concat(x, ', ') FROM (SELECT k2.stack || '@' || k2.freight AS x FROM v_carried_stack k2
                                       WHERE k2.stage = s.stage AND k2.freight = fr.freight ORDER BY k2.stack)) AS partial_detail
FROM v_freight fr
CROSS JOIN v_stage s
LEFT JOIN v_gate_eval ge ON ge.stage = s.stage
LEFT JOIN v_desired   d  ON d.stage  = s.stage
LEFT JOIN v_carried   k  ON k.stage  = s.stage
LEFT JOIN v_reversal  rv ON rv.stage = s.stage AND rv.freight = fr.freight;

-- A freight the stage moved off to an older one is `rolled-back` (whether it
-- had been carried or was still in flight); a freight the stage simply moved
-- past stays `reached` — passing through is ordinary history.
CREATE VIEW v_lanes AS
SELECT b.*,
  CASE WHEN b.reached_at IS NOT NULL AND b.is_current THEN 'reached'
       WHEN b.inflight_since IS NOT NULL  THEN 'in-flight'
       WHEN b.rolled_back_at IS NOT NULL
            AND (b.reached_at IS NOT NULL OR b.last_outcome = 'abandoned') THEN 'rolled-back'
       WHEN b.reached_at IS NOT NULL      THEN 'reached'
       WHEN b.awaiting IS NOT NULL        THEN 'awaiting'
       WHEN b.last_outcome = 'failed'     THEN 'failed'
       WHEN b.last_outcome = 'abandoned'  THEN 'superseded'
       WHEN b.n_stacks_at > 0             THEN 'partial'
       ELSE 'none' END AS cell
FROM v_lanes_base b;

-- Where is my change: PR → every freight that contains it → lanes.
CREATE VIEW v_trace AS
SELECT m.warehouse, m.pr, m.title, m.author, m.introduced_in, m.freight, fr.release_pr,
       l.stage, l.ord, l.reached_at, l.inflight_since, l.awaiting, l.awaiting_since, l.is_current,
       l.last_outcome, l.last_outcome_at, l.rolled_back_at, l.rolled_back_to
FROM v_membership m
JOIN v_freight fr ON fr.freight = m.freight
JOIN v_lanes   l  ON l.freight  = m.freight;

-- One cell per (PR, stage): the earliest freight that carried it there, and
-- whether the stage's current freight contains it now (membership is
-- cumulative, so a PR is "in production" iff production's freight has it).
CREATE VIEW v_trace_cell_base AS
SELECT t.warehouse, t.pr, t.stage, t.ord,
       min(t.reached_at) AS reached_at,
       (SELECT t2.freight FROM v_trace t2 WHERE t2.warehouse = t.warehouse AND t2.pr = t.pr AND t2.stage = t.stage AND t2.reached_at IS NOT NULL
          ORDER BY t2.reached_at, t2.freight LIMIT 1) AS via,
       max(t.inflight_since) AS inflight_since,
       (SELECT t2.awaiting FROM v_trace t2 WHERE t2.warehouse = t.warehouse AND t2.pr = t.pr AND t2.stage = t.stage AND t2.awaiting IS NOT NULL
          ORDER BY t2.awaiting_since, t2.freight LIMIT 1) AS awaiting,
       (SELECT t2.last_outcome FROM v_trace t2 WHERE t2.warehouse = t.warehouse AND t2.pr = t.pr AND t2.stage = t.stage AND t2.last_outcome IS NOT NULL
          ORDER BY t2.last_outcome_at DESC, t2.freight DESC LIMIT 1) AS last_outcome,
       max(t.is_current) AS is_current,
       max(t.rolled_back_at) AS rolled_back_at,
       (SELECT t2.rolled_back_to FROM v_trace t2 WHERE t2.warehouse = t.warehouse AND t2.pr = t.pr AND t2.stage = t.stage AND t2.rolled_back_at IS NOT NULL
          ORDER BY t2.rolled_back_at DESC LIMIT 1) AS rolled_back_to
FROM v_trace t
GROUP BY t.warehouse, t.pr, t.stage;

CREATE VIEW v_trace_cell AS
SELECT b.*,
  CASE WHEN b.reached_at IS NOT NULL AND b.is_current THEN 'reached'
       WHEN b.inflight_since IS NOT NULL  THEN 'in-flight'
       WHEN b.rolled_back_at IS NOT NULL AND NOT b.is_current
            AND (b.reached_at IS NOT NULL OR b.last_outcome = 'abandoned') THEN 'rolled-back'
       WHEN b.reached_at IS NOT NULL      THEN 'reached'
       WHEN b.awaiting IS NOT NULL        THEN 'awaiting'
       WHEN b.last_outcome = 'failed'     THEN 'failed'
       WHEN b.last_outcome = 'abandoned'  THEN 'superseded'
       ELSE 'none' END AS cell
FROM v_trace_cell_base b;

-- One line per PR: how far it got, via which freight, what it waits on next.
CREATE VIEW v_trace_summary AS
SELECT t.warehouse, t.pr, t.title, t.author, t.introduced_in,
  (SELECT c.stage FROM v_trace_cell c WHERE c.warehouse = t.warehouse AND c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_stage,
  (SELECT c.reached_at FROM v_trace_cell c WHERE c.warehouse = t.warehouse AND c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_at,
  (SELECT c.via FROM v_trace_cell c WHERE c.warehouse = t.warehouse AND c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_via,
  (SELECT c.stage || ': ' || c.awaiting FROM v_trace_cell c
     WHERE c.warehouse = t.warehouse AND c.pr = t.pr AND c.awaiting IS NOT NULL
       AND c.ord > coalesce((SELECT max(c2.ord) FROM v_trace_cell c2 WHERE c2.warehouse = t.warehouse AND c2.pr = t.pr AND c2.reached_at IS NOT NULL), 0)
     ORDER BY c.ord LIMIT 1) AS next,
  -- the train it shipped in: the earliest cut freight that contains it
  (SELECT fr.release_pr FROM v_freight fr JOIN v_membership m2 ON m2.freight = fr.freight
     WHERE m2.warehouse = t.warehouse AND m2.pr = t.pr AND fr.cut_at IS NOT NULL ORDER BY fr.cut_at LIMIT 1) AS shipped_in,
  CASE WHEN EXISTS (SELECT 1 FROM v_freight fr2 WHERE fr2.warehouse = t.warehouse AND fr2.cut_at IS NOT NULL)
        AND NOT EXISTS (SELECT 1 FROM v_freight fr JOIN v_membership m2 ON m2.freight = fr.freight
                         WHERE m2.warehouse = t.warehouse AND m2.pr = t.pr AND fr.cut_at IS NOT NULL)
       THEN 'not in a release yet (next cut picks it up)' END AS note,
  -- stages that carried this change and then rolled back off it
  (SELECT group_concat(c.stage || ' (' || c.rolled_back_at || ', to ' || c.rolled_back_to || ')', '; ')
     FROM (SELECT c.stage, c.rolled_back_at, c.rolled_back_to FROM v_trace_cell c
            WHERE c.warehouse = t.warehouse AND c.pr = t.pr AND c.cell = 'rolled-back' ORDER BY c.ord) c) AS rolled_back_from
FROM v_trace t
GROUP BY t.warehouse, t.pr;

-- Published output records: latest version per producer.
CREATE VIEW v_record AS
SELECT substr(f.subject, 8)                         AS producer,
       json_extract(f.payload, '$.version')         AS version,
       f.payload                                    AS payload,
       json_extract(f.payload, '$.produced_by')     AS produced_by,
       f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'output.published'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Uptake is intent on the edge: latest decision per edge.
CREATE VIEW v_uptaken AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.record_version')  AS version,
       json_extract(f.payload, '$.via')             AS via,
       json_extract(f.payload, '$.pr')              AS pr,
       f.actor, f.ts, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'uptake.decided'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Edge instances: the bindings that actually carry records (patterns are the
-- per-program declarations they were expanded from). The consumer names a
-- stack at a stage, `stack@stage`; a by-version pin names only the program.
CREATE VIEW v_edge_instance AS
SELECT e.*,
       CASE WHEN instr(e.consumer, '@') > 0 THEN substr(e.consumer, instr(e.consumer, '@') + 1) END AS consumer_stage,
       CASE WHEN instr(e.consumer, '@') > 0 THEN substr(e.consumer, 1, instr(e.consumer, '@') - 1) ELSE e.consumer END AS consumer_stack
FROM v_edge e
WHERE e.role = 'instance';

CREATE VIEW v_edge_term AS
SELECT e.edge,
       t.key                                        AS idx,
       json_extract(t.value, '$.type')              AS type,
       json_extract(t.value, '$.role')              AS role,
       json_extract(t.value, '$.via')               AS via
FROM v_edge_instance e, json_each(e.terms) t;

-- Hyper-previews: the consumer's plan against a proposed record, computed
-- ahead of any uptake. Latest per (consumer stage, producer, version).
CREATE VIEW v_record_plan AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.against_record.producer') AS producer,
       json_extract(f.payload, '$.against_record.version')  AS version,
       json_extract(f.payload, '$.create')          AS n_create,
       json_extract(f.payload, '$.update')          AS n_update,
       json_extract(f.payload, '$.delete')          AS n_delete,
       json_extract(f.payload, '$.replace')         AS n_replace,
       json_extract(f.payload, '$.note')            AS note,
       CASE WHEN json_extract(f.payload, '$.delete') IS NULL
              OR json_extract(f.payload, '$.replace') IS NULL
              OR json_extract(f.payload, '$.migrations_changed') IS NULL THEN NULL
            ELSE (json_extract(f.payload, '$.delete') = 0
                  AND json_extract(f.payload, '$.replace') = 0
                  AND NOT json_extract(f.payload, '$.migrations_changed')) END AS safe,
       f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'plan.summarized'
  AND json_extract(f.payload, '$.against_record') IS NOT NULL
  AND f.seq = (SELECT max(g.seq) FROM facts g
               WHERE g.kind = f.kind AND g.subject = f.subject
                 AND json_extract(g.payload, '$.against_record.producer') = json_extract(f.payload, '$.against_record.producer')
                 AND json_extract(g.payload, '$.against_record.version')  = json_extract(f.payload, '$.against_record.version'));

-- Uptake is a promotion on the edge, so its gate is the same kind of thing as
-- a stage's: typed terms, evaluated at rest against the latest published
-- record. Only the term types that make sense on an edge are defined here —
-- `verified` and `carried` name stages and freight, which an uptake has neither of.
CREATE VIEW v_uptake_term AS
SELECT e.edge, e.consumer_stage, r.version, t.idx, t.type, t.role,
  CASE t.type
    WHEN 'approved' THEN
      (SELECT a.ts FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_hold_active h WHERE h.stage = e.consumer_stage) THEN '' END
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT pl.ts FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version AND pl.safe = 1),
               (SELECT a.ts FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role ORDER BY a.seq DESC LIMIT 1))
  END AS satisfied_at,
  CASE t.type
    WHEN 'approved'              THEN 'approved by ' || t.role || coalesce(' (' || t.via || ')', '')
    WHEN 'not_held'              THEN 'no active hold on ' || e.consumer_stage
    WHEN 'plan_safe_or_approved' THEN 'preview safe, or approved by ' || t.role
    ELSE t.type || ' (not defined on an edge)'
  END AS label,
  CASE t.type
    WHEN 'approved'              THEN 'approval: ' || t.role
    WHEN 'not_held'              THEN 'hold until ' || (SELECT h.until_ts FROM v_hold_active h WHERE h.stage = e.consumer_stage)
    WHEN 'plan_safe_or_approved' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version)
           THEN 'preview of ' || e.consumer || ' against v' || r.version
           ELSE 'approval: ' || t.role || ' (preview not safe)' END
    ELSE t.type || ' is not defined on an edge'
  END AS unmet_text,
  CASE t.type
    WHEN 'not_held'              THEN (SELECT h.placed_at FROM v_hold_active h WHERE h.stage = e.consumer_stage)
    WHEN 'plan_safe_or_approved' THEN (SELECT pl.ts FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version)
  END AS onset_at,
  CASE t.type
    WHEN 'plan_safe_or_approved' THEN
      CASE WHEN EXISTS (SELECT 1 FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version AND pl.safe = 1) THEN 'auto'
           WHEN EXISTS (SELECT 1 FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role) THEN 'approved'
           WHEN NOT EXISTS (SELECT 1 FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version) THEN 'no-plan'
           ELSE 'open' END
  END AS term_outcome,
  CASE t.type
    WHEN 'approved' THEN
      (SELECT a.fact FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.facts FROM v_hold_active h WHERE h.stage = e.consumer_stage)
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT a.fact FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role ORDER BY a.seq DESC LIMIT 1),
               (SELECT pl.fact FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version))
  END AS evidence_fact,
  CASE t.type
    WHEN 'approved' THEN
      (SELECT a.actor || ' via ' || a.via FROM v_edge_approval a WHERE a.edge = e.edge AND a.version = r.version AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.holders || ': ' || h.rationale FROM v_hold_active h WHERE h.stage = e.consumer_stage)
    WHEN 'plan_safe_or_approved' THEN
      (SELECT 'preview +' || pl.n_create || ' ~' || pl.n_update || ' −' || pl.n_delete || ' ±' || pl.n_replace
              || CASE WHEN pl.safe = 1 THEN ' — safe' ELSE ' — not safe' END
         FROM v_record_plan pl WHERE pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version)
  END AS evidence
FROM v_edge_instance e
JOIN v_record r ON r.producer = e.producer
JOIN v_edge_term t ON t.edge = e.edge
WHERE e.kind = 'by-reference';

-- The uptake gate for each by-reference edge instance that declares terms,
-- against the latest published record. An edge without terms has no gate here:
-- its `uptake` mode says who decides, in prose.
CREATE VIEW v_uptake_gate AS
SELECT e.edge, r.version,
  (SELECT count(*) FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version) AS n_terms,
  (SELECT count(*) FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NULL) AS n_unmet,
  ((SELECT count(*) FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NULL) = 0) AS passes,
  (SELECT g.unmet_text FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NULL ORDER BY g.idx LIMIT 1) AS awaiting,
  (SELECT g.type FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NULL ORDER BY g.idx LIMIT 1) AS awaiting_type,
  max(coalesce((SELECT max(g.satisfied_at) FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NOT NULL), ''),
      coalesce((SELECT g.onset_at FROM v_uptake_term g WHERE g.edge = e.edge AND g.version = r.version AND g.satisfied_at IS NULL ORDER BY g.idx LIMIT 1), ''),
      coalesce(r.ts, '')) AS gate_since
FROM v_edge_instance e
JOIN v_record r ON r.producer = e.producer
WHERE e.kind = 'by-reference' AND e.terms IS NOT NULL;

-- Publication is evidence; uptake is intent. A pending uptake is the gap —
-- one row per by-reference edge instance, with its gate and the preview of
-- the consumer against the proposed record when one has been computed.
-- NULL pending = nothing has ever been published on this edge.
CREATE VIEW v_pending_uptake AS
SELECT e.edge, e.consumer, e.consumer_stage, e.consumer_stack, e.producer, e.key, e.kind, e.uptake AS policy,
       e.terms, e.rule, e.safe_rule, e.environment, e.consumer_program, e.producer_program, e.pattern,
       r.version                                              AS published_version,
       r.ts                                                   AS published_at,
       json_extract(r.payload, '$.values.' || e.key)          AS published_value,
       r.fact                                                 AS published_fact,
       u.version                                              AS consumed_version,
       u.ts                                                   AS consumed_at,
       u.actor                                                AS consumed_by,
       u.rationale                                            AS consumed_rationale,
       u.fact                                                 AS consumed_fact,
       CASE WHEN r.version IS NULL THEN NULL
            ELSE (r.version > coalesce(u.version, 0)) END      AS pending,
       g.n_terms, g.n_unmet, g.passes, g.awaiting, g.awaiting_type, g.gate_since,
       pl.fact AS preview_fact, pl.ts AS preview_at, pl.freight AS preview_freight, pl.note AS preview_note,
       pl.n_create AS p_create, pl.n_update AS p_update, pl.n_delete AS p_delete, pl.n_replace AS p_replace,
       pl.safe AS preview_safe,
       e.description
FROM v_edge_instance e
LEFT JOIN v_record      r  ON r.producer = e.producer
LEFT JOIN v_uptaken     u  ON u.edge = e.edge
LEFT JOIN v_uptake_gate g  ON g.edge = e.edge AND g.version = r.version
LEFT JOIN v_record_plan pl ON pl.stage = e.consumer_stage AND pl.producer = e.producer AND pl.version = r.version
WHERE e.kind = 'by-reference';

-- By-version bindings: the producer publishes a stage-invariant record; the
-- consumer pins a version in its config, so the uptake is a config change
-- (a PR) that rides the consumer's own freight through its ordinary gates.
-- "Pending" here means published but not yet pinned; where the pin has got to
-- is a lanes question (v_pin_stage).
CREATE VIEW v_pin_uptake AS
SELECT e.edge, e.consumer, e.consumer_program, e.producer, e.producer_program, e.key, e.uptake AS policy, e.description,
       r.version                                              AS published_version,
       r.ts                                                   AS published_at,
       r.fact                                                 AS published_fact,
       json_extract(r.payload, '$.values.' || e.key)          AS published_value,
       json_extract(r.payload, '$.note')                      AS published_note,
       u.version                                              AS pinned_version,
       u.ts                                                   AS pinned_at,
       u.actor                                                AS pinned_by,
       u.via                                                  AS pinned_via,
       u.pr                                                   AS pinned_pr,
       u.fact                                                 AS pinned_fact,
       (SELECT fr.freight FROM v_freight fr JOIN v_warehouse w ON w.name = fr.warehouse AND w.program = e.consumer_program
         WHERE json_extract(fr.config, '$.' || e.key) = r.version ORDER BY fr.discovered_at, fr.seq LIMIT 1) AS pinned_in,
       (SELECT fr.discovered_at FROM v_freight fr JOIN v_warehouse w ON w.name = fr.warehouse AND w.program = e.consumer_program
         WHERE json_extract(fr.config, '$.' || e.key) = r.version ORDER BY fr.discovered_at, fr.seq LIMIT 1) AS pinned_in_at,
       CASE WHEN r.version IS NULL THEN NULL
            ELSE (r.version > coalesce(u.version, 0)) END      AS pending
FROM v_edge_instance e
LEFT JOIN v_record  r ON r.producer = e.producer
LEFT JOIN v_uptaken u ON u.edge = e.edge
WHERE e.kind = 'by-version';

-- Where the pin has got to: for each consumer stage, the pin its carried
-- freight holds, and the lane cell of the freight that carries the new pin.
CREATE VIEW v_pin_stage AS
SELECT p.edge, p.key, p.published_version, p.pinned_in, s.stage, s.environment, s.ord,
       k.freight                                    AS carried,
       json_extract(fr.config, '$.' || p.key)       AS carried_pin,
       l.cell, l.reached_at, l.awaiting, l.awaiting_since, l.inflight_since, l.last_outcome, l.last_outcome_at, l.is_current,
       l.current_since, l.rolled_back_at, l.rolled_back_to,
       CASE WHEN p.published_version IS NULL                                        THEN 'nothing'
            WHEN json_extract(fr.config, '$.' || p.key) = p.published_version       THEN 'current'
            WHEN p.pinned_in IS NULL                                                THEN 'unpinned'
            ELSE 'behind' END                       AS pin_state
FROM v_pin_uptake p
JOIN v_stage s ON s.program = p.consumer_program
LEFT JOIN v_carried k  ON k.stage = s.stage
LEFT JOIN v_freight fr ON fr.freight = k.freight
LEFT JOIN v_lanes   l  ON l.stage = s.stage AND l.freight = p.pinned_in;

-- Impact is a queue, not a feature: everything downstream of a producer along
-- declared edges, transitively, with each edge's uptake state right now.
CREATE VIEW v_impact AS
WITH RECURSIVE down(root, node, edge, depth, path) AS (
  SELECT DISTINCT e.producer, e.producer, NULL, 0, e.producer FROM v_edge_instance e
  UNION ALL
  SELECT d.root, e.consumer, e.edge, d.depth + 1, d.path || ' → ' || e.consumer
  FROM down d JOIN v_edge_instance e ON e.producer = d.node
  WHERE d.depth < 8 AND instr(d.path, e.consumer) = 0
)
SELECT d.root, d.node AS consumer, d.edge, d.depth, d.path,
       e.kind, e.uptake AS policy, e.key, e.consumer_program, e.producer_program,
       coalesce(pu.pending, pi.pending)             AS pending,
       coalesce(pu.published_version, pi.published_version) AS published_version,
       coalesce(pu.consumed_version, pi.pinned_version)     AS consumed_version,
       pu.passes, pu.awaiting, pi.pinned_in
FROM down d
JOIN v_edge_instance e ON e.edge = d.edge
LEFT JOIN v_pending_uptake pu ON pu.edge = d.edge
LEFT JOIN v_pin_uptake     pi ON pi.edge = d.edge
WHERE d.depth > 0;

-- A pin-set (composite freight): one intent fact naming a member freight per
-- program. Enactment is the members' own promotions under their own teams'
-- policies; the pin-set only says which set is meant to be verified together.
CREATE VIEW v_pinset AS
SELECT substr(f.subject, 9)                         AS release,
       json_extract(f.payload, '$.display')         AS display,
       json_extract(f.payload, '$.members')         AS members,
       json_extract(f.payload, '$.order')           AS ord,
       f.ts AS pinned_at, f.actor AS pinned_by, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'release.pinned'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

CREATE VIEW v_pinset_member AS
SELECT p.release, je.key AS program, je.value AS freight
FROM v_pinset p, json_each(p.members) je;

-- Each member at each environment: the member program's stage there, whether it
-- carries the pinned freight now, and the lane cell of that freight.
CREATE VIEW v_pinset_env AS
SELECT m.release, m.program, m.freight, s.environment, s.stage, s.ord,
       (k.freight IS m.freight)                     AS carried_now,
       (d.freight IS m.freight)                     AS desired_now,
       l.cell, l.reached_at, l.awaiting, l.awaiting_since, l.inflight_since, l.last_outcome, l.last_outcome_at, l.is_current,
       l.current_since, l.rolled_back_at, l.rolled_back_to
FROM v_pinset_member m
JOIN v_stage s ON s.program = m.program
LEFT JOIN v_lanes   l ON l.freight = m.freight AND l.stage = s.stage
LEFT JOIN v_carried k ON k.stage = s.stage
LEFT JOIN v_desired d ON d.stage = s.stage;

CREATE VIEW v_pinset_status AS
SELECT release, environment, min(ord) AS ord,
       count(*)                                     AS members,
       sum(carried_now)                             AS members_carried,
       CASE WHEN sum(carried_now) = count(*) THEN 'complete'
            WHEN sum(carried_now) > 0       THEN 'partial'
            ELSE 'pending' END                      AS state,
       CASE WHEN sum(carried_now) = count(*) THEN max(reached_at) END AS complete_at
FROM v_pinset_env
GROUP BY release, environment;

-- The estate: program × environment, with what each stage is wired with (the
-- record versions its by-reference edges have taken up) and how many uptakes
-- wait on it.
CREATE VIEW v_estate AS
SELECT g.*,
       (SELECT group_concat(x, ', ') FROM (SELECT pu.key || ' v' || pu.consumed_version AS x FROM v_pending_uptake pu
                                            WHERE pu.consumer_stage = g.stage AND pu.consumed_version IS NOT NULL ORDER BY pu.key)) AS wired,
       (SELECT count(*) FROM v_pending_uptake pu WHERE pu.consumer_stage = g.stage AND pu.pending = 1) AS pending_uptakes,
       (SELECT count(*) FROM v_pending_uptake pu
         WHERE substr(pu.producer, instr(pu.producer, '@') + 1) = g.stage AND pu.pending = 1) AS pending_downstream
FROM v_grid g;

-- Past releases: Keith's release cards. The card itself is the cut freight;
-- its per-stage cells come from v_release_stage, pivoted by the renderer over
-- whatever stages exist (no stage is named here).
CREATE VIEW v_releases AS
SELECT fr.freight, fr.release_pr, fr.release_branch, fr.release_title, fr.cut_at, fr.sha,
       (SELECT count(*) FROM v_release_prs rp WHERE rp.freight = fr.freight)                                AS prs,
       (SELECT group_concat('#' || pr, ' ') FROM (SELECT rp.pr FROM v_release_prs rp WHERE rp.freight = fr.freight
                                                   ORDER BY rp.pr DESC))                                     AS pr_list
FROM v_freight fr
WHERE fr.cut_at IS NOT NULL
ORDER BY fr.cut_at DESC;

CREATE VIEW v_release_stage AS
SELECT fr.freight, s.stage, s.ord, l.reached_at, l.cell, l.awaiting, l.awaiting_since, l.inflight_since,
       l.last_outcome, l.last_outcome_at, l.is_current, l.current_since, l.rolled_back_at, l.rolled_back_to,
       (SELECT a.actor FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight ORDER BY a.seq DESC LIMIT 1) AS approved_by,
       (SELECT a.ts    FROM v_approval a WHERE a.stage = s.stage AND a.freight = fr.freight ORDER BY a.seq DESC LIMIT 1) AS approved_at,
       EXISTS (SELECT 1 FROM v_policy_term t WHERE t.stage = s.stage AND t.type IN ('approved', 'plan_safe_or_approved')) AS approvable
FROM v_freight fr
CROSS JOIN v_stage s
JOIN v_lanes l ON l.freight = fr.freight AND l.stage = s.stage
WHERE fr.cut_at IS NOT NULL;

-- Audit: every promotion decision, checked against the policy that should
-- have written it. The ledger records what it is told; this is how a rogue
-- or unevidenced decision stays distinguishable from a legitimate one.
--
-- The approval-bearing terms (approved, plan_safe_or_approved) are checked
-- as of the decision's own timestamp — was a still-valid approval, or a
-- still-current safe plan, on record when the decision was written? — in the
-- direction the decision had at that instant. The other terms (verified,
-- carried, not_held, previously_carried) are replayed by render.py's
-- per-decision check; expressing them as-of-T in SQL wants every "latest"
-- view parameterised by T (smells.md).
CREATE VIEW v_audit_ctx AS
SELECT d.fact AS decision, d.stage, d.freight, d.ts, d.seq, d.actor,
       -- the incumbent when the decision was written: the latest successful
       -- freight enactment on the stage at or before then
       inc.freight AS incumbent,
       CASE WHEN inc.discovered_at IS NOT NULL AND f.discovered_at < inc.discovered_at THEN 'rollback' ELSE 'forward' END AS direction,
       -- when intent had last moved off this freight, as of the decision
       (SELECT max(o.ts) FROM v_decision o
         WHERE o.stage = d.stage AND o.freight <> d.freight AND o.seq < d.seq
           AND o.seq > (SELECT min(p.seq) FROM v_decision p WHERE p.stage = d.stage AND p.freight = d.freight)) AS moved_at
FROM v_decision d
JOIN v_freight f ON f.freight = d.freight
LEFT JOIN v_freight inc ON inc.freight = (SELECT o.freight FROM v_freight_transition o
                                           WHERE o.stage = d.stage AND o.outcome = 'succeeded' AND o.freight IS NOT NULL
                                             AND o.finished_at <= d.ts ORDER BY o.finished_seq DESC LIMIT 1);

CREATE VIEW v_audit_term AS
SELECT c.decision, c.stage, c.freight, c.ts, c.direction, t.idx, t.type, t.role,
  CASE t.type
    WHEN 'approved' THEN
      (SELECT a.fact FROM v_approval a
        WHERE a.stage = c.stage AND a.freight = c.freight AND a.role = t.role
          AND a.ts <= c.ts AND a.ts >= coalesce(c.moved_at, '') ORDER BY a.seq DESC LIMIT 1)
    WHEN 'plan_safe_or_approved' THEN
      coalesce(
        -- the latest plan on record at decision time, if it was safe and still
        -- current (no other freight had been carried since it was computed)
        (SELECT CASE WHEN json_extract(p.payload, '$.delete') = 0 AND json_extract(p.payload, '$.replace') = 0
                          AND NOT json_extract(p.payload, '$.migrations_changed') THEN p.id END
           FROM facts p
          WHERE p.kind = 'plan.summarized' AND p.subject = 'stage:' || c.stage
            AND json_extract(p.payload, '$.against_record') IS NULL
            AND json_extract(p.payload, '$.freight') = c.freight AND p.ts <= c.ts
            AND NOT EXISTS (SELECT 1 FROM v_freight_transition o
                             WHERE o.stage = c.stage AND o.outcome = 'succeeded' AND o.freight IS NOT NULL
                               AND o.freight <> c.freight AND o.finished_at > p.ts AND o.finished_at <= c.ts)
          ORDER BY p.seq DESC LIMIT 1),
        (SELECT a.fact FROM v_approval a
          WHERE a.stage = c.stage AND a.freight = c.freight AND a.role = t.role
            AND a.ts <= c.ts AND a.ts >= coalesce(c.moved_at, '') ORDER BY a.seq DESC LIMIT 1))
  END AS satisfied_by,
  CASE t.type WHEN 'approved' THEN 'approval: ' || t.role ELSE 'safe plan or approval: ' || t.role END AS requirement
FROM v_audit_ctx c
JOIN v_policy_term t ON t.stage = c.stage AND t.direction = c.direction AND t.type IN ('approved', 'plan_safe_or_approved');

CREATE VIEW v_audit_decision AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       f.ts, f.actor, f.rationale, f.id AS fact,
       json_array_length(f.refs)                    AS n_refs,
       c.direction, c.incumbent,
       CASE WHEN c.direction = 'rollback' THEN coalesce(p.rollback_mode, p.mode) ELSE p.mode END AS mode,
       (SELECT count(*) FROM v_audit_term x WHERE x.decision = f.id)                              AS n_required,
       (SELECT count(*) FROM v_audit_term x WHERE x.decision = f.id AND x.satisfied_by IS NULL)   AS n_unmet,
       (SELECT group_concat(x.requirement, '; ') FROM v_audit_term x
         WHERE x.decision = f.id AND x.satisfied_by IS NULL)                                      AS unmet,
       (SELECT group_concat(x.satisfied_by, ' ') FROM v_audit_term x
         WHERE x.decision = f.id AND x.satisfied_by IS NOT NULL)                                  AS evidence
FROM facts f
JOIN v_policy p ON p.stage = substr(f.subject, 7)
JOIN v_audit_ctx c ON c.decision = f.id
WHERE f.kind = 'promotion.decided';

CREATE VIEW v_audit_flag AS
SELECT d.*,
  CASE
    WHEN d.actor NOT LIKE 'policy:%' THEN 'decided by ' || d.actor || ' directly, not by the stage policy'
    WHEN d.n_unmet > 0               THEN 'unmet at decision time: ' || d.unmet
    WHEN d.n_refs = 0                THEN 'no evidence cited'
  END AS flag
FROM v_audit_decision d;

-- The same audit for uptake decisions: was every approval-bearing term of the
-- edge's policy met, on record, at the instant the uptake was written?
CREATE VIEW v_uptake_audit_term AS
SELECT d.id AS decision, d.subject AS edge, json_extract(d.payload, '$.record_version') AS version, d.ts,
       t.idx, t.type, t.role, e.consumer_stage, e.producer,
  CASE t.type
    WHEN 'approved' THEN
      (SELECT a.fact FROM v_edge_approval a
        WHERE a.edge = d.subject AND a.version = json_extract(d.payload, '$.record_version')
          AND a.role = t.role AND a.ts <= d.ts ORDER BY a.seq DESC LIMIT 1)
    WHEN 'plan_safe_or_approved' THEN
      coalesce(
        (SELECT CASE WHEN json_extract(p.payload, '$.delete') = 0 AND json_extract(p.payload, '$.replace') = 0
                          AND NOT json_extract(p.payload, '$.migrations_changed') THEN p.id END
           FROM facts p
          WHERE p.kind = 'plan.summarized' AND p.subject = 'stage:' || e.consumer_stage
            AND json_extract(p.payload, '$.against_record.producer') = e.producer
            AND json_extract(p.payload, '$.against_record.version') = json_extract(d.payload, '$.record_version')
            AND p.ts <= d.ts
          ORDER BY p.seq DESC LIMIT 1),
        (SELECT a.fact FROM v_edge_approval a
          WHERE a.edge = d.subject AND a.version = json_extract(d.payload, '$.record_version')
            AND a.role = t.role AND a.ts <= d.ts ORDER BY a.seq DESC LIMIT 1))
  END AS satisfied_by,
  CASE t.type WHEN 'approved' THEN 'approval: ' || t.role ELSE 'safe preview or approval: ' || t.role END AS requirement
FROM facts d
JOIN v_edge_instance e ON e.edge = d.subject
JOIN v_edge_term t ON t.edge = d.subject AND t.type IN ('approved', 'plan_safe_or_approved')
WHERE d.kind = 'uptake.decided';

CREATE VIEW v_uptake_audit_flag AS
SELECT d.subject AS edge, json_extract(d.payload, '$.record_version') AS version, d.ts, d.actor, d.rationale, d.id AS fact,
       json_array_length(d.refs) AS n_refs, e.uptake AS mode, e.consumer,
       (SELECT count(*) FROM v_uptake_audit_term x WHERE x.decision = d.id)                            AS n_required,
       (SELECT count(*) FROM v_uptake_audit_term x WHERE x.decision = d.id AND x.satisfied_by IS NULL) AS n_unmet,
       (SELECT group_concat(requirement, '; ') FROM (SELECT x.requirement FROM v_uptake_audit_term x WHERE x.decision = d.id AND x.satisfied_by IS NULL ORDER BY x.idx)) AS unmet,
       (SELECT group_concat(satisfied_by, ' ') FROM (SELECT x.satisfied_by FROM v_uptake_audit_term x WHERE x.decision = d.id AND x.satisfied_by IS NOT NULL ORDER BY x.idx)) AS evidence,
  CASE
    WHEN d.actor NOT LIKE 'policy:%' AND e.terms IS NOT NULL THEN 'decided by ' || d.actor || ' directly, not by the edge policy'
    WHEN (SELECT count(*) FROM v_uptake_audit_term x WHERE x.decision = d.id AND x.satisfied_by IS NULL) > 0
         THEN 'unmet at decision time: ' || (SELECT group_concat(x.requirement, '; ') FROM v_uptake_audit_term x WHERE x.decision = d.id AND x.satisfied_by IS NULL)
    WHEN json_array_length(d.refs) = 0 THEN 'no evidence cited'
  END AS flag
FROM facts d
JOIN v_edge_instance e ON e.edge = d.subject
WHERE d.kind = 'uptake.decided';

-- Every fact, tagged with the stage it belongs to (directly, or through its
-- transition). The "what happened" view is this, filtered.
CREATE VIEW v_timeline AS
SELECT f.seq, f.ts, f.class, f.kind, f.subject, f.actor, f.payload, f.rationale, f.refs, f.id,
       coalesce(CASE WHEN f.subject LIKE 'stage:%' THEN substr(f.subject, 7) END, t.stage) AS stage
FROM facts f
LEFT JOIN v_transition t ON t.transition = f.subject;
