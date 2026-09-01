-- Views over the ledger. Every screen the renderer shows is one of these.
--
-- Layering: subjects (latest declaration wins) → per-subject current state
-- (latest fact of a kind) → derived states (candidate, gate, drift) → screens
-- (grid, lanes, trace, uptake, releases). Nothing here is stored; delete the
-- facts and every view is empty.

---------------------------------------------------------------------------
-- Subjects: latest declaration wins
---------------------------------------------------------------------------

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
       json_extract(f.payload, '$.verify_in')       AS verify_in,   -- the stage whose verifications this gate reads
       json_extract(f.payload, '$.stacks')          AS stacks,
       f.ts                                         AS declared_at,
       f.actor                                      AS declared_by,
       f.id                                         AS fact
FROM facts f
WHERE f.kind = 'stage.declared'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'stage.declared' AND g.subject = f.subject);

CREATE VIEW v_policy AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.mode')            AS mode,
       json_extract(f.payload, '$.trigger')         AS trigger,
       json_extract(f.payload, '$.rule')            AS rule,
       json_extract(f.payload, '$.safe')            AS safe_rule,
       json_extract(f.payload, '$.description')     AS description,
       f.ts                                         AS declared_at,
       f.id                                         AS fact
FROM facts f
WHERE f.kind = 'policy.declared'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'policy.declared' AND g.subject = f.subject);

CREATE VIEW v_edge AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.consumer')        AS consumer,
       json_extract(f.payload, '$.producer')        AS producer,
       json_extract(f.payload, '$.key')             AS key,
       json_extract(f.payload, '$.uptake')          AS uptake,
       json_extract(f.payload, '$.description')     AS description,
       f.id                                         AS fact
FROM facts f
WHERE f.kind = 'binding.declared'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'binding.declared' AND g.subject = f.subject);

-- Freight is discovered (observation) and may later be cut as a release (intent).
CREATE VIEW v_freight AS
SELECT substr(f.subject, 9)                         AS freight,
       json_extract(f.payload, '$.source.sha')      AS sha,
       json_extract(f.payload, '$.source.branch')   AS branch,
       json_extract(f.payload, '$.build')           AS build,
       json_extract(f.payload, '$.prs')             AS prs,
       json_extract(f.payload, '$.images')          AS images,
       json_extract(f.payload, '$.config')          AS config,
       f.ts                                         AS discovered_at,
       f.id                                         AS fact,
       c.ts                                         AS cut_at,
       json_extract(c.payload, '$.release_pr')      AS release_pr,
       json_extract(c.payload, '$.branch')          AS release_branch,
       json_extract(c.payload, '$.armed_by')        AS cut_armed_by,
       c.id                                         AS cut_fact
FROM facts f
LEFT JOIN facts c ON c.kind = 'release.cut' AND c.subject = f.subject
WHERE f.kind = 'freight.discovered';

---------------------------------------------------------------------------
-- Per-subject current state: latest fact of a kind
---------------------------------------------------------------------------

-- Intent: what each stage should carry (latest decision wins).
CREATE VIEW v_desired AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       f.ts                                         AS decided_at,
       f.actor                                      AS decided_by,
       f.rationale,
       f.id                                         AS fact
FROM facts f
WHERE f.kind = 'promotion.decided'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'promotion.decided' AND g.subject = f.subject);

-- A transition is the enactment: started once, phases and steps in between,
-- finished at most once.
CREATE VIEW v_transition AS
SELECT s.subject                                    AS transition,
       json_extract(s.payload, '$.stage')           AS stage,
       json_extract(s.payload, '$.freight')         AS freight,
       json_extract(s.payload, '$.stack')           AS stack,
       json_extract(s.payload, '$.record_version')  AS record_version,
       json_extract(s.payload, '$.run')             AS run,
       json_extract(s.payload, '$.strategy')        AS strategy,
       s.ts                                         AS started_at,
       e.ts                                         AS finished_at,
       json_extract(e.payload, '$.outcome')         AS outcome,
       json_extract(e.payload, '$.ops_update')      AS ops_update,
       json_extract(e.payload, '$.summary')         AS summary,
       (SELECT json_extract(p.payload, '$.phase') FROM facts p
         WHERE p.kind = 'transition.phase' AND p.subject = s.subject
         ORDER BY p.ts DESC LIMIT 1)                AS last_phase,
       (SELECT count(*) FROM facts r
         WHERE r.kind = 'resource.step' AND r.subject = s.subject) AS resource_steps,
       s.id                                         AS started_fact,
       e.id                                         AS finished_fact
FROM facts s
LEFT JOIN facts e ON e.kind = 'transition.finished' AND e.subject = s.subject
WHERE s.kind = 'transition.started';

-- Observation: what each stage actually carries — the latest enactment that
-- finished successfully for the service stack.
CREATE VIEW v_carried AS
SELECT t.stage, t.freight, t.finished_at AS since, t.ops_update, t.transition,
       t.finished_fact AS fact
FROM v_transition t
WHERE t.stack = 'service' AND t.outcome = 'succeeded'
  AND t.finished_at = (SELECT max(u.finished_at) FROM v_transition u
                       WHERE u.stage = t.stage AND u.stack = 'service'
                         AND u.outcome = 'succeeded');

CREATE VIEW v_inflight AS
SELECT * FROM v_transition WHERE finished_at IS NULL;

CREATE VIEW v_last_finished AS
SELECT t.* FROM v_transition t
WHERE t.stack = 'service' AND t.finished_at IS NOT NULL
  AND t.finished_at = (SELECT max(u.finished_at) FROM v_transition u
                       WHERE u.stage = t.stage AND u.stack = 'service'
                         AND u.finished_at IS NOT NULL);

-- Verification status per (stage, freight, check): latest outcome wins.
CREATE VIEW v_verified AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.check')           AS chk,
       json_extract(f.payload, '$.outcome')         AS outcome,
       json_extract(f.payload, '$.detail')          AS detail,
       f.actor, f.ts, f.id                          AS fact
FROM facts f
WHERE f.kind = 'verification.recorded'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'verification.recorded' AND g.subject = f.subject
                AND json_extract(g.payload, '$.freight') = json_extract(f.payload, '$.freight')
                AND json_extract(g.payload, '$.check')   = json_extract(f.payload, '$.check'));

CREATE VIEW v_approval AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.freight')         AS freight,
       json_extract(f.payload, '$.role')            AS role,
       json_extract(f.payload, '$.via')             AS via,
       f.actor, f.ts, f.rationale, f.id             AS fact
FROM facts f
WHERE f.kind = 'approval.granted';

-- Holds are intent facts with an expiry; "active" is relative to the clock.
CREATE VIEW v_hold AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.until')           AS until_ts,
       json_extract(f.payload, '$.scope')           AS scope,
       f.actor, f.ts AS placed_at, f.rationale, f.id AS fact
FROM facts f
WHERE f.kind = 'hold.placed'
  AND json_extract(f.payload, '$.until') > (SELECT now FROM clock);

-- Plan facts are stage-3 of the key decomposition: (desired, world@T) → plan.
-- "safe" is the production-eu policy's safe-rule, evaluated here.
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
       (json_extract(f.payload, '$.delete') = 0
        AND json_extract(f.payload, '$.replace') = 0
        AND NOT json_extract(f.payload, '$.migrations_changed')) AS safe,
       f.ts, f.id                                   AS fact
FROM facts f
WHERE f.kind = 'plan.summarized'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'plan.summarized' AND g.subject = f.subject
                AND json_extract(g.payload, '$.freight') = json_extract(f.payload, '$.freight'));

-- Conformance reads: what a watch last saw running, compared to intent.
CREATE VIEW v_observed AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.services')        AS services,
       json_extract(f.payload, '$.source')          AS source,
       f.ts, f.id                                   AS fact,
       (SELECT count(*) FROM json_each(json_extract(f.payload, '$.services')) je
         WHERE je.value <> (SELECT d.freight FROM v_desired d WHERE d.stage = substr(f.subject, 7)))
                                                    AS mismatches,
       (SELECT group_concat(je.key || '@' || je.value, ', ')
          FROM json_each(json_extract(f.payload, '$.services')) je
         WHERE je.value <> (SELECT d.freight FROM v_desired d WHERE d.stage = substr(f.subject, 7)))
                                                    AS mismatch_detail
FROM facts f
WHERE f.kind = 'state.observed'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'state.observed' AND g.subject = f.subject);

-- Break-glass is the side door: same class (intent), same attribution.
CREATE VIEW v_breakglass AS
SELECT substr(f.subject, 7)                         AS stage,
       json_extract(f.payload, '$.scope')           AS scope,
       json_extract(f.payload, '$.action')          AS action,
       json_extract(f.payload, '$.from_freight')    AS from_freight,
       json_extract(f.payload, '$.to_freight')      AS to_freight,
       json_extract(f.payload, '$.incident')        AS incident,
       json_extract(f.payload, '$.expiry')          AS expiry,
       f.actor, f.ts, f.rationale, f.id             AS fact
FROM facts f
WHERE f.kind = 'breakglass.recorded'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'breakglass.recorded' AND g.subject = f.subject);

---------------------------------------------------------------------------
-- Derived: candidate, gate, drift
---------------------------------------------------------------------------

-- The candidate is what each stage's upstream offers it right now.
CREATE VIEW v_candidate AS
SELECT s.stage,
  CASE
    WHEN s.upstream = 'warehouse:master' THEN
      (SELECT fr.freight FROM v_freight fr WHERE fr.branch = 'master' ORDER BY fr.discovered_at DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.freight FROM v_freight fr WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC LIMIT 1)
    ELSE
      (SELECT k.freight FROM v_carried k WHERE k.stage = s.upstream)
  END AS freight,
  CASE
    WHEN s.upstream = 'warehouse:master' THEN
      (SELECT fr.discovered_at FROM v_freight fr WHERE fr.branch = 'master' ORDER BY fr.discovered_at DESC LIMIT 1)
    WHEN s.upstream = 'release-train' THEN
      (SELECT fr.cut_at FROM v_freight fr WHERE fr.cut_at IS NOT NULL ORDER BY fr.cut_at DESC LIMIT 1)
    ELSE
      (SELECT k.since FROM v_carried k WHERE k.stage = s.upstream)
  END AS available_at
FROM v_stage s;

-- Gate inputs for every (stage, freight) pair: each policy rule's terms as
-- columns, each with the fact time that satisfies it (NULL = unmet). This is
-- what makes "would this gate pass right now?" answerable with no execution.
CREATE VIEW v_gate_all AS
SELECT s.stage, fr.freight, p.mode, p.rule,
  (SELECT v.ts FROM v_verified v
    WHERE v.stage = s.verify_in AND v.freight = fr.freight
      AND v.chk = 'integration-tests' AND v.outcome = 'pass')  AS upstream_integration_at,
  (SELECT v.ts FROM v_verified v
    WHERE v.stage = 'staging' AND v.freight = fr.freight
      AND v.chk = 'smoke' AND v.outcome = 'pass')              AS staging_smoke_at,
  (SELECT v.ts FROM v_verified v
    WHERE v.stage = 'staging' AND v.freight = fr.freight
      AND v.chk = 'load-generator' AND v.outcome = 'pass')     AS staging_loadgen_at,
  (SELECT k.since FROM v_carried k
    WHERE k.stage = s.upstream AND k.freight = fr.freight)      AS upstream_carried_at,
  (SELECT a.ts FROM v_approval a
    WHERE a.stage = s.stage AND a.freight = fr.freight)         AS approved_at,
  (SELECT a.actor FROM v_approval a
    WHERE a.stage = s.stage AND a.freight = fr.freight)         AS approved_by,
  (SELECT a.fact FROM v_approval a
    WHERE a.stage = s.stage AND a.freight = fr.freight)         AS approval_fact,
  (SELECT pl.safe FROM v_plan pl
    WHERE pl.stage = s.stage AND pl.freight = fr.freight)       AS plan_safe,
  (SELECT pl.migrations_changed FROM v_plan pl
    WHERE pl.stage = s.stage AND pl.freight = fr.freight)       AS migrations_changed,
  (SELECT pl.fact FROM v_plan pl
    WHERE pl.stage = s.stage AND pl.freight = fr.freight)       AS plan_fact,
  (SELECT h.until_ts FROM v_hold h WHERE h.stage = s.stage)     AS held_until,
  (SELECT h.fact FROM v_hold h WHERE h.stage = s.stage)         AS hold_fact
FROM v_stage s
CROSS JOIN v_freight fr
JOIN v_policy p ON p.stage = s.stage;

-- The rules, compiled to SQL by hand (see smells.md: an expression evaluator
-- over the fact schema would generate this from v_policy.rule). Per stage:
-- does the candidate pass, and if not, what is it waiting on, since when.
CREATE VIEW v_gate_eval AS
SELECT g.*, c.available_at,
  CASE g.stage
    WHEN 'testing'       THEN 1
    WHEN 'testing-eu'    THEN 1
    WHEN 'staging'       THEN (g.upstream_integration_at IS NOT NULL)
    WHEN 'production'    THEN (g.upstream_integration_at IS NOT NULL
                               AND g.staging_smoke_at IS NOT NULL
                               AND g.staging_loadgen_at IS NOT NULL
                               AND g.approved_at IS NOT NULL)
    WHEN 'production-eu' THEN (g.upstream_carried_at IS NOT NULL
                               AND g.upstream_integration_at IS NOT NULL
                               AND g.held_until IS NULL
                               AND (g.plan_safe = 1 OR g.approved_at IS NOT NULL))
  END AS passes,
  CASE g.stage
    WHEN 'staging' THEN
      CASE WHEN g.upstream_integration_at IS NULL THEN 'verification: integration-tests in testing' END
    WHEN 'production' THEN
      CASE WHEN g.upstream_integration_at IS NULL THEN 'verification: integration-tests in staging'
           WHEN g.staging_smoke_at IS NULL        THEN 'verification: smoke in staging'
           WHEN g.staging_loadgen_at IS NULL      THEN 'verification: load-generator in staging'
           WHEN g.approved_at IS NULL             THEN 'approval: oncall (merge release PR)' END
    WHEN 'production-eu' THEN
      CASE WHEN g.upstream_carried_at IS NULL     THEN 'production to carry ' || g.freight
           WHEN g.upstream_integration_at IS NULL THEN 'verification: integration-tests in production'
           WHEN g.held_until IS NOT NULL          THEN 'hold until ' || g.held_until
           WHEN g.plan_safe IS NULL               THEN 'plan for production-eu'
           WHEN g.plan_safe = 0 AND g.approved_at IS NULL
                                                  THEN 'approval: oncall (plan not safe: migrations or destructive steps)' END
  END AS awaiting,
  max(coalesce(c.available_at, ''),
      coalesce(g.upstream_integration_at, ''),
      coalesce(g.staging_smoke_at, ''),
      coalesce(g.staging_loadgen_at, ''),
      coalesce(g.upstream_carried_at, '')) AS awaiting_since
FROM v_gate_all g
JOIN v_candidate c ON c.stage = g.stage AND c.freight = g.freight;

---------------------------------------------------------------------------
-- Screens
---------------------------------------------------------------------------

-- The subject grid: one row per stage — desired, carried, and the derived
-- status. No status is stored anywhere; every column is a join.
CREATE VIEW v_grid AS
SELECT s.ord, s.stage, s.region, s.owner, s.url, s.ops,
       d.freight        AS desired,
       d.decided_at,
       d.decided_by,
       k.freight        AS carried,
       k.since          AS carried_since,
       k.ops_update,
       i.transition     AS inflight,
       i.last_phase,
       ge.freight       AS candidate,
       ge.awaiting,
       ge.awaiting_since,
       ge.held_until,
       CASE
         WHEN i.transition IS NOT NULL                             THEN 'in-flight'
         WHEN lf.outcome = 'failed' AND d.freight <> k.freight     THEN 'failed'
         WHEN ge.freight IS NOT NULL AND ge.freight <> d.freight
              AND ge.held_until IS NOT NULL                        THEN 'held'
         WHEN ge.freight IS NOT NULL AND ge.freight <> d.freight
              AND ge.passes = 0                                    THEN 'awaiting'
         WHEN ge.freight IS NOT NULL AND ge.freight <> d.freight
              AND ge.passes = 1                                    THEN 'ready'
         WHEN d.freight = k.freight                                THEN 'converged'
         ELSE 'pending'
       END AS status,
       CASE WHEN o.mismatches > 0 THEN
         o.mismatch_detail ||
         CASE WHEN bg.fact IS NOT NULL
              THEN ' — break-glass ' || bg.incident || ' (' || bg.fact || ')'
              ELSE ' — UNEXPLAINED' END
       END AS drift,
       o.ts AS observed_at,
       h.until_ts AS hold_until, h.actor AS hold_by, h.rationale AS hold_rationale
FROM v_stage s
LEFT JOIN v_desired       d  ON d.stage  = s.stage
LEFT JOIN v_carried       k  ON k.stage  = s.stage
LEFT JOIN v_inflight      i  ON i.stage  = s.stage AND i.stack = 'service'
LEFT JOIN v_last_finished lf ON lf.stage = s.stage
LEFT JOIN v_gate_eval     ge ON ge.stage = s.stage
LEFT JOIN v_observed      o  ON o.stage  = s.stage
LEFT JOIN v_breakglass    bg ON bg.stage = s.stage AND bg.ts > coalesce(k.since, '')
LEFT JOIN v_hold          h  ON h.stage  = s.stage
ORDER BY s.ord;

-- Freight lanes: freight × stage, long form. The renderer pivots it.
CREATE VIEW v_lanes AS
SELECT fr.freight, fr.release_pr, fr.cut_at, fr.discovered_at, s.stage, s.ord,
  (SELECT min(t.finished_at) FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight
      AND t.stack = 'service' AND t.outcome = 'succeeded')       AS reached_at,
  (SELECT t.started_at FROM v_transition t
    WHERE t.stage = s.stage AND t.freight = fr.freight
      AND t.stack = 'service' AND t.finished_at IS NULL)         AS inflight_since,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0
        AND (d.freight IS NULL OR d.freight <> fr.freight)
       THEN ge.awaiting END                                      AS awaiting,
  CASE WHEN ge.freight = fr.freight AND ge.passes = 0
        AND (d.freight IS NULL OR d.freight <> fr.freight)
       THEN ge.awaiting_since END                                AS awaiting_since,
  (k.freight = fr.freight)                                       AS is_current
FROM v_freight fr
CROSS JOIN v_stage s
LEFT JOIN v_gate_eval ge ON ge.stage = s.stage
LEFT JOIN v_desired   d  ON d.stage  = s.stage
LEFT JOIN v_carried   k  ON k.stage  = s.stage;

-- Where is my change: PR → freight → lanes. Keith's trace, as a join.
CREATE VIEW v_trace AS
SELECT json_extract(pr.value, '$.number') AS pr,
       json_extract(pr.value, '$.title')  AS title,
       json_extract(pr.value, '$.author') AS author,
       fr.freight, fr.release_pr,
       l.stage, l.ord, l.reached_at, l.inflight_since, l.awaiting, l.awaiting_since, l.is_current
FROM v_freight fr, json_each(fr.prs) pr
JOIN v_lanes l ON l.freight = fr.freight;

-- One line per PR: how far it got, and what it is waiting on next.
CREATE VIEW v_trace_summary AS
SELECT t.pr, t.title, t.author, t.freight, t.release_pr,
  (SELECT t2.stage FROM v_trace t2 WHERE t2.pr = t.pr AND t2.reached_at IS NOT NULL
     ORDER BY t2.ord DESC LIMIT 1)                               AS furthest_stage,
  (SELECT t2.reached_at FROM v_trace t2 WHERE t2.pr = t.pr AND t2.reached_at IS NOT NULL
     ORDER BY t2.ord DESC LIMIT 1)                               AS furthest_at,
  (SELECT t2.stage || ': ' || t2.awaiting || ' (since ' || t2.awaiting_since || ')'
     FROM v_trace t2 WHERE t2.pr = t.pr AND t2.awaiting IS NOT NULL
     ORDER BY t2.ord LIMIT 1)                                    AS next,
  CASE WHEN t.release_pr IS NULL THEN 'not in a release yet (next cut picks it up)' END AS note
FROM v_trace t
GROUP BY t.pr;

-- Published output records: latest version per producer.
CREATE VIEW v_record AS
SELECT substr(f.subject, 8)                         AS producer,
       json_extract(f.payload, '$.version')         AS version,
       f.payload                                    AS payload,
       json_extract(f.payload, '$.produced_by')     AS produced_by,
       f.ts, f.id                                   AS fact
FROM facts f
WHERE f.kind = 'output.published'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'output.published' AND g.subject = f.subject);

-- Uptake is intent on the edge: latest decision per edge.
CREATE VIEW v_uptaken AS
SELECT f.subject                                    AS edge,
       json_extract(f.payload, '$.record_version')  AS version,
       f.actor, f.ts, f.rationale, f.id             AS fact
FROM facts f
WHERE f.kind = 'uptake.decided'
  AND f.ts = (SELECT max(g.ts) FROM facts g
              WHERE g.kind = 'uptake.decided' AND g.subject = f.subject);

-- Publication is evidence; uptake is intent. A pending uptake is the gap.
CREATE VIEW v_pending_uptake AS
SELECT e.consumer, e.producer, e.key, e.uptake AS policy,
       r.version                                              AS published_version,
       r.ts                                                   AS published_at,
       json_extract(r.payload, '$.values.' || e.key)          AS published_value,
       r.fact                                                 AS published_fact,
       u.version                                              AS consumed_version,
       u.ts                                                   AS consumed_at,
       u.actor                                                AS consumed_by,
       (r.version > coalesce(u.version, 0))                   AS pending,
       'preview ' || e.consumer || ' with ' || e.key || ' = '
         || json_extract(r.payload, '$.values.' || e.key)     AS preview,
       e.description
FROM v_edge e
LEFT JOIN v_record  r ON r.producer = e.producer
LEFT JOIN v_uptaken u ON u.edge = e.edge;

-- Past releases: Keith's release cards, as a query over lanes and approvals.
CREATE VIEW v_releases AS
SELECT fr.freight, fr.release_pr, fr.release_branch, fr.cut_at, fr.sha,
       json_array_length(fr.prs)                                          AS prs,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'staging')       AS staging_at,
       (SELECT a.actor FROM v_approval a WHERE a.stage = 'production' AND a.freight = fr.freight)      AS approved_by,
       (SELECT a.ts    FROM v_approval a WHERE a.stage = 'production' AND a.freight = fr.freight)      AS approved_at,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'production')    AS production_at,
       (SELECT l.reached_at FROM v_lanes l WHERE l.freight = fr.freight AND l.stage = 'production-eu') AS production_eu_at
FROM v_freight fr
WHERE fr.cut_at IS NOT NULL
ORDER BY fr.cut_at DESC;

-- Every fact, tagged with the stage it belongs to (directly, or through its
-- transition). The "what happened" view is this, filtered.
CREATE VIEW v_timeline AS
SELECT f.seq, f.ts, f.class, f.kind, f.subject, f.actor, f.payload, f.rationale, f.refs, f.id,
       coalesce(CASE WHEN f.subject LIKE 'stage:%' THEN substr(f.subject, 7) END, t.stage) AS stage
FROM facts f
LEFT JOIN v_transition t ON t.transition = f.subject;
