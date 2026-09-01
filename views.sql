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
       f.ts AS declared_at, f.id AS fact
FROM facts f
WHERE f.kind = 'warehouse.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

CREATE VIEW v_stage AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.order')           AS ord,
       json_extract(f.payload, '$.display')         AS display,
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
       f.ts AS declared_at, f.id AS fact
FROM facts f
WHERE f.kind = 'policy.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- One row per gate term, in the order the policy lists them. The term types
-- are the vocabulary a gate-expression language would need (OPEN 4).
CREATE VIEW v_policy_term AS
SELECT p.stage,
       t.key                                        AS idx,
       json_extract(t.value, '$.type')              AS type,
       json_extract(t.value, '$.stage')             AS term_stage,
       json_extract(t.value, '$.check')             AS chk,
       json_extract(t.value, '$.role')              AS role,
       json_extract(t.value, '$.via')               AS via
FROM v_policy p, json_each(p.terms) t;

CREATE VIEW v_edge AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.consumer')        AS consumer,
       json_extract(f.payload, '$.producer')        AS producer,
       json_extract(f.payload, '$.key')             AS key,
       json_extract(f.payload, '$.uptake')          AS uptake,
       json_extract(f.payload, '$.description')     AS description,
       f.id AS fact
FROM facts f
WHERE f.kind = 'binding.declared'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Freight is discovered (observation) and may later be cut as a release (intent).
CREATE VIEW v_freight AS
SELECT substr(f.subject, 9)                         AS freight,
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

-- Membership is cumulative along a branch: a PR merged to master is in every
-- later master build. `prs` on a freight lists what that build introduced.
CREATE VIEW v_membership AS
SELECT later.freight,
       json_extract(pr.value, '$.number')           AS pr,
       json_extract(pr.value, '$.title')            AS title,
       json_extract(pr.value, '$.author')           AS author,
       earlier.freight                              AS introduced_in,
       earlier.discovered_at                        AS merged_at
FROM v_freight later
JOIN v_freight earlier ON earlier.branch = later.branch
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
                              WHERE p.cut_at IS NOT NULL AND p.cut_at < r.cut_at), '');

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

-- Observation: what each stage actually carries — the latest enactment of the
-- service stack that finished successfully.
CREATE VIEW v_carried AS
SELECT t.stage, t.freight, t.finished_at AS since, t.ops_update, t.transition, t.finished_fact AS fact
FROM v_transition t
WHERE t.stack = 'service' AND t.outcome = 'succeeded'
  AND t.finished_seq = (SELECT max(u.finished_seq) FROM v_transition u
                        WHERE u.stage = t.stage AND u.stack = 'service' AND u.outcome = 'succeeded');

CREATE VIEW v_inflight AS
SELECT t.* FROM v_transition t
WHERE t.stack = 'service' AND t.finished_at IS NULL
  AND t.started_seq = (SELECT max(u.started_seq) FROM v_transition u
                       WHERE u.stage = t.stage AND u.stack = 'service' AND u.finished_at IS NULL);

CREATE VIEW v_last_finished AS
SELECT t.* FROM v_transition t
WHERE t.stack = 'service' AND t.finished_at IS NOT NULL
  AND t.finished_seq = (SELECT max(u.finished_seq) FROM v_transition u
                        WHERE u.stage = t.stage AND u.stack = 'service' AND u.finished_at IS NOT NULL);

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

-- Every approval, with its role. Gates pick the latest matching one.
CREATE VIEW v_approval AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.role')            AS role,
       json_extract(f.payload, '$.via')             AS via,
       f.actor, f.ts, f.seq, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'approval.granted';

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
       f.ts, f.id AS fact
FROM facts f
WHERE f.kind = 'plan.summarized'
  AND f.seq = (SELECT max(g.seq) FROM facts g
               WHERE g.kind = f.kind AND g.subject = f.subject
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

-- The candidate is what each stage's upstream offers it right now.
CREATE VIEW v_candidate AS
SELECT s.stage,
  CASE
    WHEN s.upstream LIKE 'warehouse:%' THEN
      (SELECT fr.freight FROM v_freight fr JOIN v_warehouse w ON w.name = substr(s.upstream, 11) AND fr.branch = w.branch
        ORDER BY fr.discovered_at DESC, fr.seq DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.freight FROM v_freight fr WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC, fr.seq DESC LIMIT 1)
    ELSE
      (SELECT k.freight FROM v_carried k WHERE k.stage = s.upstream)
  END AS freight,
  CASE
    WHEN s.upstream LIKE 'warehouse:%' THEN
      (SELECT fr.discovered_at FROM v_freight fr JOIN v_warehouse w ON w.name = substr(s.upstream, 11) AND fr.branch = w.branch
        ORDER BY fr.discovered_at DESC, fr.seq DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.cut_at FROM v_freight fr WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC, fr.seq DESC LIMIT 1)
    ELSE
      (SELECT k.since FROM v_carried k WHERE k.stage = s.upstream)
  END AS available_at
FROM v_stage s;

-- Every gate term evaluated for every (stage, freight) pair: satisfied_at is
-- the time of the fact that satisfies it (NULL = unmet). A verification only
-- counts once the stage actually carried the freight — you verify what ran.
-- This is what makes "would this gate pass right now?" answerable at rest.
CREATE VIEW v_gate_term AS
SELECT s.stage, fr.freight, t.idx, t.type, t.term_stage, t.chk, t.role,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.ts FROM v_verified v
        WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk AND v.outcome = 'pass'
          AND v.ts >= coalesce((SELECT min(x.finished_at) FROM v_transition x
                                WHERE x.stage = t.term_stage AND x.freight = fr.freight
                                  AND x.stack = 'service' AND x.outcome = 'succeeded'), '9999'))
    WHEN 'carried' THEN
      (SELECT k.since FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.ts FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      CASE WHEN NOT EXISTS (SELECT 1 FROM v_hold_active h WHERE h.stage = s.stage) THEN '' END
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT pl.ts FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight AND pl.safe = 1),
               (SELECT a.ts FROM v_approval a
                 WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role ORDER BY a.seq DESC LIMIT 1))
  END AS satisfied_at,
  CASE t.type
    WHEN 'verified'              THEN 'verified in ' || t.term_stage || ': ' || t.chk
    WHEN 'carried'               THEN 'carried by ' || t.term_stage
    WHEN 'approved'              THEN 'approved by ' || t.role || coalesce(' (' || t.via || ')', '')
    WHEN 'not_held'              THEN 'no active hold'
    WHEN 'plan_safe_or_approved' THEN 'plan safe, or approved by ' || t.role
  END AS label,
  CASE t.type
    WHEN 'verified' THEN
      CASE WHEN EXISTS (SELECT 1 FROM v_verified v WHERE v.stage = t.term_stage AND v.freight = fr.freight
                          AND v.chk = t.chk AND v.outcome = 'pass'
                          AND v.ts < coalesce((SELECT min(x.finished_at) FROM v_transition x
                                               WHERE x.stage = t.term_stage AND x.freight = fr.freight
                                                 AND x.stack = 'service' AND x.outcome = 'succeeded'), '9999'))
           THEN 'verification: ' || t.chk || ' in ' || t.term_stage || ' (recorded before ' || t.term_stage || ' carried ' || fr.freight || ' — re-run)'
           ELSE 'verification: ' || t.chk || ' in ' || t.term_stage END
    WHEN 'carried'               THEN t.term_stage || ' to carry ' || fr.freight
    WHEN 'approved'              THEN 'approval: ' || t.role
    WHEN 'not_held'              THEN 'hold until ' || (SELECT h.until_ts FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      CASE WHEN (SELECT pl.fact FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight) IS NULL
           THEN 'plan for ' || s.stage
           ELSE 'approval: ' || t.role || ' (plan not safe)' END
  END AS unmet_text,
  -- when the wait on an unmet term began, where the term has an onset of its own
  CASE t.type
    WHEN 'not_held'              THEN (SELECT h.placed_at FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN (SELECT pl.ts FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight)
  END AS onset_at,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.fact FROM v_verified v WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk)
    WHEN 'carried' THEN
      (SELECT k.fact FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.fact FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.facts FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      coalesce((SELECT a.fact FROM v_approval a
                 WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role ORDER BY a.seq DESC LIMIT 1),
               (SELECT pl.fact FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight))
  END AS evidence_fact,
  CASE t.type
    WHEN 'verified' THEN
      (SELECT v.outcome || ': ' || v.detail FROM v_verified v
        WHERE v.stage = t.term_stage AND v.freight = fr.freight AND v.chk = t.chk)
    WHEN 'carried' THEN
      (SELECT 'since ' || k.since FROM v_carried k WHERE k.stage = t.term_stage AND k.freight = fr.freight)
    WHEN 'approved' THEN
      (SELECT a.actor || ' via ' || a.via FROM v_approval a
        WHERE a.stage = s.stage AND a.freight = fr.freight AND a.role = t.role ORDER BY a.seq DESC LIMIT 1)
    WHEN 'not_held' THEN
      (SELECT h.holders || ': ' || h.rationale FROM v_hold_active h WHERE h.stage = s.stage)
    WHEN 'plan_safe_or_approved' THEN
      (SELECT 'plan +' || pl.n_create || ' ~' || pl.n_update || ' −' || pl.n_delete || ' ±' || pl.n_replace
              || CASE WHEN pl.migrations_changed THEN ', migrations' ELSE '' END
              || CASE WHEN pl.safe = 1 THEN ' — safe' ELSE ' — not safe' END
         FROM v_plan pl WHERE pl.stage = s.stage AND pl.freight = fr.freight)
  END AS evidence
FROM v_stage s
CROSS JOIN v_freight fr
JOIN v_policy_term t ON t.stage = s.stage;

-- The gate for every (stage, freight): passes iff no term is unmet; awaiting
-- is the first unmet term; gate_since is when the wait on it began.
CREATE VIEW v_gate AS
SELECT s.stage, fr.freight, p.mode, p.rule,
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
JOIN v_policy p ON p.stage = s.stage;

-- The gate as it applies to each stage's candidate.
CREATE VIEW v_gate_eval AS
SELECT g.*, c.available_at,
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
SELECT s.ord, s.stage, s.region, s.owner, s.url, s.ops,
       d.freight AS desired, d.decided_at, d.decided_by,
       k.freight AS carried, k.since AS carried_since, k.ops_update,
       i.transition AS inflight, i.freight AS inflight_freight, i.last_phase, i.started_at AS inflight_since,
       lf.outcome AS last_outcome, lf.freight AS last_outcome_freight, lf.finished_at AS last_outcome_at,
       lf.error AS last_error, lf.transition AS last_transition, lf.failed_step, lf.step_url,
       ge.freight AS candidate, ge.passes, ge.awaiting, ge.awaiting_type, ge.awaiting_since,
       CASE
         WHEN i.transition IS NOT NULL AND i.freight IS NOT d.freight              THEN 'superseded'
         WHEN i.transition IS NOT NULL                                              THEN 'in-flight'
         WHEN lf.outcome = 'failed' AND lf.freight IS d.freight
              AND d.freight IS NOT k.freight                                        THEN 'failed'
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
LEFT JOIN v_carried       k  ON k.stage  = s.stage
LEFT JOIN v_inflight      i  ON i.stage  = s.stage
LEFT JOIN v_last_finished lf ON lf.stage = s.stage
LEFT JOIN v_gate_eval     ge ON ge.stage = s.stage
LEFT JOIN v_observed      o  ON o.stage  = s.stage
LEFT JOIN v_breakglass    bg ON bg.stage = s.stage AND bg.ts > coalesce(k.since, '')
LEFT JOIN v_hold_active   h  ON h.stage  = s.stage
ORDER BY s.ord;

-- Freight lanes: freight × stage, long form. The renderer pivots it.
CREATE VIEW v_lanes AS
SELECT fr.freight, fr.release_pr, fr.cut_at, fr.discovered_at, s.stage, s.ord,
  (SELECT min(t.finished_at) FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.stack = 'service' AND t.outcome = 'succeeded') AS reached_at,
  (SELECT t.started_at FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.stack = 'service' AND t.finished_at IS NULL
    ORDER BY t.started_seq DESC LIMIT 1) AS inflight_since,
  (SELECT t.outcome FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.stack = 'service' AND t.finished_at IS NOT NULL
    ORDER BY t.finished_seq DESC LIMIT 1) AS last_outcome,
  (SELECT t.finished_at FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight AND t.stack = 'service' AND t.finished_at IS NOT NULL
    ORDER BY t.finished_seq DESC LIMIT 1) AS last_outcome_at,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0 AND d.freight IS NOT fr.freight
       THEN ge.awaiting END AS awaiting,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0 AND d.freight IS NOT fr.freight
       THEN ge.awaiting_since END AS awaiting_since,
  (k.freight IS fr.freight) AS is_current
FROM v_freight fr
CROSS JOIN v_stage s
LEFT JOIN v_gate_eval ge ON ge.stage = s.stage
LEFT JOIN v_desired   d  ON d.stage  = s.stage
LEFT JOIN v_carried   k  ON k.stage  = s.stage;

-- Where is my change: PR → every freight that contains it → lanes.
CREATE VIEW v_trace AS
SELECT m.pr, m.title, m.author, m.introduced_in, m.freight, fr.release_pr,
       l.stage, l.ord, l.reached_at, l.inflight_since, l.awaiting, l.awaiting_since, l.is_current,
       l.last_outcome, l.last_outcome_at
FROM v_membership m
JOIN v_freight fr ON fr.freight = m.freight
JOIN v_lanes   l  ON l.freight  = m.freight;

-- One cell per (PR, stage): the earliest freight that carried it there.
CREATE VIEW v_trace_cell AS
SELECT t.pr, t.stage, t.ord,
       min(t.reached_at) AS reached_at,
       (SELECT t2.freight FROM v_trace t2 WHERE t2.pr = t.pr AND t2.stage = t.stage AND t2.reached_at IS NOT NULL
          ORDER BY t2.reached_at LIMIT 1) AS via,
       max(t.inflight_since) AS inflight_since,
       (SELECT t2.awaiting FROM v_trace t2 WHERE t2.pr = t.pr AND t2.stage = t.stage AND t2.awaiting IS NOT NULL
          ORDER BY t2.awaiting_since LIMIT 1) AS awaiting,
       (SELECT t2.last_outcome FROM v_trace t2 WHERE t2.pr = t.pr AND t2.stage = t.stage AND t2.last_outcome IS NOT NULL
          ORDER BY t2.last_outcome_at DESC LIMIT 1) AS last_outcome
FROM v_trace t
GROUP BY t.pr, t.stage;

-- One line per PR: how far it got, via which freight, what it waits on next.
CREATE VIEW v_trace_summary AS
SELECT t.pr, t.title, t.author, t.introduced_in,
  (SELECT c.stage FROM v_trace_cell c WHERE c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_stage,
  (SELECT c.reached_at FROM v_trace_cell c WHERE c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_at,
  (SELECT c.via FROM v_trace_cell c WHERE c.pr = t.pr AND c.reached_at IS NOT NULL ORDER BY c.ord DESC LIMIT 1) AS furthest_via,
  (SELECT c.stage || ': ' || c.awaiting FROM v_trace_cell c
     WHERE c.pr = t.pr AND c.awaiting IS NOT NULL
       AND c.ord > coalesce((SELECT max(c2.ord) FROM v_trace_cell c2 WHERE c2.pr = t.pr AND c2.reached_at IS NOT NULL), 0)
     ORDER BY c.ord LIMIT 1) AS next,
  -- the train it shipped in: the earliest cut freight that contains it
  (SELECT fr.release_pr FROM v_freight fr JOIN v_membership m2 ON m2.freight = fr.freight
     WHERE m2.pr = t.pr AND fr.cut_at IS NOT NULL ORDER BY fr.cut_at LIMIT 1) AS shipped_in,
  CASE WHEN NOT EXISTS (SELECT 1 FROM v_freight fr JOIN v_membership m2 ON m2.freight = fr.freight
                         WHERE m2.pr = t.pr AND fr.cut_at IS NOT NULL)
       THEN 'not in a release yet (next cut picks it up)' END AS note
FROM v_trace t
GROUP BY t.pr;

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
       f.actor, f.ts, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'uptake.decided'
  AND f.seq = (SELECT max(g.seq) FROM facts g WHERE g.kind = f.kind AND g.subject = f.subject);

-- Publication is evidence; uptake is intent. A pending uptake is the gap.
-- NULL pending = nothing has ever been published on this edge.
CREATE VIEW v_pending_uptake AS
SELECT e.consumer, e.producer, e.key, e.uptake AS policy,
       r.version                                              AS published_version,
       r.ts                                                   AS published_at,
       json_extract(r.payload, '$.values.' || e.key)          AS published_value,
       r.fact                                                 AS published_fact,
       u.version                                              AS consumed_version,
       u.ts                                                   AS consumed_at,
       u.actor                                                AS consumed_by,
       CASE WHEN r.version IS NULL THEN NULL
            ELSE (r.version > coalesce(u.version, 0)) END      AS pending,
       'preview ' || e.consumer || ' with ' || e.key || ' = '
         || json_extract(r.payload, '$.values.' || e.key)     AS preview,
       e.description
FROM v_edge e
LEFT JOIN v_record  r ON r.producer = e.producer
LEFT JOIN v_uptaken u ON u.edge = e.edge;

-- Past releases: Keith's release cards, as a query over lanes and approvals.
CREATE VIEW v_releases AS
SELECT fr.freight, fr.release_pr, fr.release_branch, fr.release_title, fr.cut_at, fr.sha,
       (SELECT count(*) FROM v_release_prs rp WHERE rp.freight = fr.freight)                                AS prs,
       (SELECT group_concat('#' || rp.pr, ' ') FROM v_release_prs rp WHERE rp.freight = fr.freight)         AS pr_list,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'staging')             AS staging_at,
       (SELECT a.actor FROM v_approval a WHERE a.stage = 'production' AND a.freight = fr.freight
          ORDER BY a.seq DESC LIMIT 1)                                                                       AS approved_by,
       (SELECT a.ts FROM v_approval a WHERE a.stage = 'production' AND a.freight = fr.freight
          ORDER BY a.seq DESC LIMIT 1)                                                                       AS approved_at,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'production')          AS production_at,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'production-eu')       AS production_eu_at
FROM v_freight fr
WHERE fr.cut_at IS NOT NULL
ORDER BY fr.cut_at DESC;

-- Audit: every promotion decision, checked against the policy that should
-- have written it. The ledger records what it is told; this is how a rogue
-- or unevidenced decision stays distinguishable from a legitimate one.
CREATE VIEW v_audit_decision AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       f.ts, f.actor, f.rationale, f.id AS fact,
       json_array_length(f.refs)                    AS n_refs,
       p.mode,
       (SELECT t.role FROM v_policy_term t WHERE t.stage = substr(f.subject, 7) AND t.type = 'approved' LIMIT 1) AS required_role,
       (SELECT a.fact FROM v_approval a
         WHERE a.stage = substr(f.subject, 7) AND a.freight = json_extract(f.payload, '$.freight')
           AND a.ts <= f.ts
           AND a.role = (SELECT t.role FROM v_policy_term t WHERE t.stage = substr(f.subject, 7) AND t.type = 'approved' LIMIT 1)
         ORDER BY a.seq DESC LIMIT 1) AS approval_fact
FROM facts f
JOIN v_policy p ON p.stage = substr(f.subject, 7)
WHERE f.kind = 'promotion.decided';

CREATE VIEW v_audit_flag AS
SELECT d.*,
  CASE
    WHEN d.actor NOT LIKE 'policy:%'                             THEN 'decided by ' || d.actor || ' directly, not by the stage policy'
    WHEN d.required_role IS NOT NULL AND d.approval_fact IS NULL THEN 'gated stage decided with no ' || d.required_role || ' approval on record'
    WHEN d.n_refs = 0                                            THEN 'no evidence cited'
  END AS flag
FROM v_audit_decision d;

-- Every fact, tagged with the stage it belongs to (directly, or through its
-- transition). The "what happened" view is this, filtered.
CREATE VIEW v_timeline AS
SELECT f.seq, f.ts, f.class, f.kind, f.subject, f.actor, f.payload, f.rationale, f.refs, f.id,
       coalesce(CASE WHEN f.subject LIKE 'stage:%' THEN substr(f.subject, 7) END, t.stage) AS stage
FROM facts f
LEFT JOIN v_transition t ON t.transition = f.subject;
