#!/usr/bin/env python3
"""facts.jsonl → sqlite → views → the tracker, as one HTML page.

    ./render.py                                   lint, self-check, render out/index.html
    ./render.py --lint-only                       lint + self-checks, no HTML
    ./render.py --as-of 2026-08-24T16:20:00Z      render the ledger as it stood then

Everything the page shows is a SELECT over the `facts` table (schema.sql,
views.sql). The renderer holds no state: it loads the facts whose timestamp is
at or before `--as-of`, sets the clock, and formats query results. Rendering
the same ledger at several instants is the cheapest proof that the ledger, not
the UI, is where state lives — the page carries a snapshot switcher for that.
"""
import argparse
import copy
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

INTENT_KINDS = {
    "warehouse.declared", "stage.declared", "policy.declared", "binding.declared", "release.cut",
    "promotion.decided", "approval.granted", "hold.placed", "breakglass.recorded", "uptake.decided",
}
OBSERVATION_KINDS = {
    "freight.discovered", "transition.started", "transition.phase", "transition.finished",
    "resource.step", "verification.recorded", "job.finished", "plan.summarized",
    "output.published", "state.observed",
}
RATIONALE_REQUIRED = {
    "release.cut", "promotion.decided", "approval.granted", "hold.placed",
    "breakglass.recorded", "uptake.decided",
}
SUBJECT_TYPES = {"warehouse", "stage", "freight", "transition", "edge", "record"}
TRANSITION_OUTCOMES = {"succeeded", "failed", "abandoned"}
PLAN_KEYS = {"create", "update", "delete", "replace", "migrations_changed"}
# Derived states must never be written down. If a fact carries one of these,
# the demo is storing what it claims to compute.
FORBIDDEN_KEYS = {"status", "state"}
FORBIDDEN_VALUES = {"awaiting", "drifted", "converged", "held", "pending", "ready", "in-flight", "superseded", "idle"}

DEFAULT_AS_OF = "2026-08-26T17:45:00Z"
SNAPSHOTS = [
    # (as_of, caption) — the primary first; the rest are the time-travel tabs.
    # Every claim in a caption is pinned by a self-check at the same instant.
    (DEFAULT_AS_OF,           "Wed 17:45 — F418 waiting on oncall; production drifted since Monday night"),
    ("2026-08-24T16:20:00Z",  "Mon 16:20 — F417 mid-rollout to production, both versions live"),
    ("2026-08-24T16:50:00Z",  "Mon 16:50 — production-eu holds F417 for approval: the plan touches a migration"),
    ("2026-08-26T16:15:00Z",  "Wed 16:15 — F419 failed in testing-eu, still rolling in testing"),
    ("2026-08-26T16:21:10Z",  "Wed 16:21 — F420 supersedes F419 mid-flight in testing"),
]


# ---------------------------------------------------------------------------
# Lint: the fixture must be a well-formed ledger before any view reads it
# ---------------------------------------------------------------------------

def load_facts(path):
    facts = []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{n}: not JSON: {e}")
    return facts


def walk(obj):
    """Yield (key, value) pairs recursively through dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def lint(facts):
    errors = []
    err = lambda i, f, msg: errors.append(f"{f.get('id', '?')} (line {i}): {msg}")
    seen_ids, last_ts = set(), ""
    declared = {"warehouse": set(), "stage": set(), "freight": set(), "edge": set()}
    transitions = {}  # subject -> {"started": bool, "finished": int}

    for i, f in enumerate(facts, 1):
        missing = [k for k in ("id", "ts", "class", "kind", "subject", "actor", "payload") if k not in f]
        if missing:
            err(i, f, f"missing {', '.join(missing)}")
            continue
        if f["id"] in seen_ids:
            err(i, f, "duplicate id")
        seen_ids.add(f["id"])
        if f["ts"] < last_ts:
            err(i, f, f"timestamp {f['ts']} earlier than previous {last_ts} — the ledger is append-only")
        last_ts = max(last_ts, f["ts"])

        cls, kind, subj, p = f["class"], f["kind"], f["subject"], f["payload"]
        if cls == "intent" and kind not in INTENT_KINDS:
            err(i, f, f"{kind} is not an intent kind")
        elif cls == "observation" and kind not in OBSERVATION_KINDS:
            err(i, f, f"{kind} is not an observation kind")
        elif cls not in ("intent", "observation"):
            err(i, f, f"class {cls}")
        stype, _, sname = subj.partition(":")
        if stype not in SUBJECT_TYPES or not sname:
            err(i, f, f"subject {subj!r} is not <type>:<name> with a known type")
        if kind in RATIONALE_REQUIRED and not f.get("rationale"):
            err(i, f, f"{kind} needs a rationale")
        if cls == "observation" and f.get("rationale"):
            err(i, f, "observations do not carry rationale — that is an intent field")

        for k, v in walk(p):
            if k in FORBIDDEN_KEYS:
                err(i, f, f"payload key {k!r}: derived state must not be stored")
            if isinstance(v, str) and v in FORBIDDEN_VALUES:
                err(i, f, f"payload value {v!r}: derived state must not be stored")

        for r in f.get("refs", []):
            if r not in seen_ids:
                err(i, f, f"ref {r} does not point at an earlier fact (missing, or out of order)")

        # Subject bookkeeping: declare before use.
        if kind == "warehouse.declared":
            declared["warehouse"].add(sname)
        elif kind == "stage.declared":
            declared["stage"].add(sname)
            up = p.get("upstream", "")
            if up.startswith("warehouse:") and up[10:] not in declared["warehouse"]:
                err(i, f, f"upstream {up} is not a declared warehouse")
        elif kind == "freight.discovered":
            declared["freight"].add(sname)
        elif kind == "binding.declared":
            declared["edge"].add(subj)

        if stype == "stage" and kind != "stage.declared" and sname not in declared["stage"]:
            err(i, f, f"stage {sname} used before declaration")
        if stype == "freight" and kind != "freight.discovered" and sname not in declared["freight"]:
            err(i, f, f"freight {sname} used before discovery")
        if stype == "edge" and kind != "binding.declared" and subj not in declared["edge"]:
            err(i, f, "edge used before its binding was declared")
        if "stage" in p and p["stage"] not in declared["stage"]:
            err(i, f, f"payload.stage {p['stage']} is not a declared stage")
        for key in ("freight", "from_freight", "to_freight"):
            if p.get(key) is not None and p[key] not in declared["freight"]:
                err(i, f, f"payload.{key} {p[key]} is not a discovered freight")

        if kind == "plan.summarized" and not PLAN_KEYS <= set(p):
            err(i, f, f"plan.summarized lacks {sorted(PLAN_KEYS - set(p))} — the safe-rule reads them")
        if kind == "transition.finished" and p.get("outcome") not in TRANSITION_OUTCOMES:
            err(i, f, f"transition outcome {p.get('outcome')!r} not in {sorted(TRANSITION_OUTCOMES)}")

        if stype == "transition":
            t = transitions.setdefault(subj, {"started": False, "finished": 0})
            if kind == "transition.started":
                if t["started"]:
                    err(i, f, "transition started twice")
                t["started"] = True
            else:
                if not t["started"]:
                    err(i, f, f"{kind} before transition.started")
                if kind == "transition.finished":
                    t["finished"] += 1
                    if t["finished"] > 1:
                        err(i, f, "transition finished twice")
    return errors


# ---------------------------------------------------------------------------
# Build: the ledger prefix at an instant
# ---------------------------------------------------------------------------

def build_db(facts, as_of):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    with open(os.path.join(HERE, "schema.sql")) as fh:
        db.executescript(fh.read())
    db.executemany(
        "INSERT INTO facts (id, ts, class, kind, subject, actor, payload, rationale, refs)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(f["id"], f["ts"], f["class"], f["kind"], f["subject"], f["actor"],
          json.dumps(f["payload"]), f.get("rationale"), json.dumps(f.get("refs", [])))
         for f in facts if f["ts"] <= as_of],
    )
    db.execute("INSERT INTO clock (now) VALUES (?)", (as_of,))
    with open(os.path.join(HERE, "views.sql")) as fh:
        db.executescript(fh.read())
    return db


def q(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(db, sql, *args):
    rows = q(db, sql, *args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Self-checks: what each view must include AND exclude. A SELECT written
# against one hand-authored fixture passes by construction; these pin the
# negative cases, and the mutation checks break one mechanism at a time to
# prove the views (not the fixture) carry the answer.
# ---------------------------------------------------------------------------

def find(facts, **match):
    """First fact whose top-level fields and payload fields match."""
    for f in facts:
        ok = True
        for k, v in match.items():
            if k.startswith("p_"):
                ok = ok and f["payload"].get(k[2:]) == v
            else:
                ok = ok and f.get(k) == v
        if ok:
            return f
    raise KeyError(match)


def mutate(facts, fn):
    """A copy of the ledger with `fn` applied, re-sorted by time (stable)."""
    fs = copy.deepcopy(facts)
    fn(fs)
    return sorted(fs, key=lambda f: f["ts"])


def grid(db, stage):
    return one(db, "SELECT * FROM v_grid WHERE stage=?", stage)


def self_checks(facts):
    checks = []  # (name, as_of, mutation-or-None, predicate)

    def check(name, as_of, fn, mutation=None):
        checks.append((name, as_of, mutation, fn))

    A = DEFAULT_AS_OF
    # --- the primary instant ------------------------------------------------
    check("grid: production awaiting oncall approval (candidate F418, desired F417), since the last verification", A,
          lambda db: (lambda r: r["status"] == "awaiting" and r["awaiting"] == "approval: oncall"
                      and r["candidate"] == "F418" and r["desired"] == "F417"
                      and r["awaiting_since"] == "2026-08-26T15:35:00Z")(grid(db, "production")))
    check("grid: production drift is explained by the break-glass fact", A,
          lambda db: "INC-2311" in (grid(db, "production")["drift"] or ""))
    check("grid: production-eu converged (candidate == desired), held, no drift", A,
          lambda db: (lambda r: r["status"] == "converged" and r["hold_until"] is not None and r["drift"] is None)
          (grid(db, "production-eu")))
    check("grid: staging converged on F418; testing and testing-eu converged on F420", A,
          lambda db: grid(db, "staging")["carried"] == "F418"
          and grid(db, "testing")["status"] == "converged" and grid(db, "testing")["carried"] == "F420"
          and grid(db, "testing-eu")["status"] == "converged" and grid(db, "testing-eu")["carried"] == "F420")
    check("grid: exactly five rows; none in-flight, failed or superseded", A,
          lambda db: len(q(db, "SELECT stage FROM v_grid")) == 5
          and q(db, "SELECT stage FROM v_grid WHERE status IN ('in-flight','failed','superseded')") == [])
    check("lanes: F418 has not reached production and is the awaiting freight there", A,
          lambda db: (lambda r: r["reached_at"] is None and r["awaiting"] is not None)
          (one(db, "SELECT * FROM v_lanes WHERE freight='F418' AND stage='production'")))
    check("lanes: F419 reached nothing — abandoned in testing, failed in testing-eu", A,
          lambda db: q(db, "SELECT * FROM v_lanes WHERE freight='F419' AND reached_at IS NOT NULL") == []
          and one(db, "SELECT last_outcome FROM v_lanes WHERE freight='F419' AND stage='testing'")["last_outcome"] == "abandoned"
          and one(db, "SELECT last_outcome FROM v_lanes WHERE freight='F419' AND stage='testing-eu'")["last_outcome"] == "failed")
    check("lanes: F417 is awaiting nowhere (it is desired everywhere it sits)", A,
          lambda db: q(db, "SELECT * FROM v_lanes WHERE freight='F417' AND awaiting IS NOT NULL") == [])
    check("gate terms: production-eu × F416 safe by plan; × F417 by the EU approval; × F418 waits on production first", A,
          lambda db: one(db, "SELECT evidence FROM v_gate_term WHERE stage='production-eu' AND freight='F416' AND type='plan_safe_or_approved'")["evidence"].endswith("— safe")
          and one(db, "SELECT evidence_fact FROM v_gate_term WHERE stage='production-eu' AND freight='F417' AND type='plan_safe_or_approved'")["evidence_fact"]
              == one(db, "SELECT fact FROM v_approval WHERE stage='production-eu' AND freight='F417'")["fact"]
          and (lambda r: r["passes"] == 0 and r["awaiting"] == "production to carry F418")
          (one(db, "SELECT * FROM v_gate WHERE stage='production-eu' AND freight='F418'")))
    check("gate: while production-eu is held, no freight passes there; F417 (what production carries) is blocked by the hold, the rest by carry", A,
          lambda db: q(db, "SELECT * FROM v_gate WHERE stage='production-eu' AND passes = 1") == []
          and one(db, "SELECT awaiting_type FROM v_gate WHERE stage='production-eu' AND freight='F417'")["awaiting_type"] == "not_held"
          and q(db, "SELECT freight FROM v_gate WHERE stage='production-eu' AND freight <> 'F417' AND awaiting_type <> 'carried'") == [])
    check("as of Mon 17:02:10 (no hold): production-eu × F417 passes by approval; × F416 no longer passes — production has moved on", "2026-08-24T17:02:10Z",
          lambda db: one(db, "SELECT passes FROM v_gate WHERE stage='production-eu' AND freight='F417'")["passes"] == 1
          and (lambda r: r["passes"] == 0 and r["awaiting"] == "production to carry F416")
          (one(db, "SELECT * FROM v_gate WHERE stage='production-eu' AND freight='F416'")))
    check("as of Aug 18 16:47:35: production-eu × F416 passed on its safe plan, no approval involved", "2026-08-18T16:47:35Z",
          lambda db: one(db, "SELECT passes FROM v_gate WHERE stage='production-eu' AND freight='F416'")["passes"] == 1
          and one(db, "SELECT evidence_fact FROM v_gate_term WHERE stage='production-eu' AND freight='F416' AND type='plan_safe_or_approved'")["evidence_fact"]
              == one(db, "SELECT fact FROM v_plan WHERE stage='production-eu' AND freight='F416'")["fact"])
    check("gate: an approval on production does not count for production-eu", A,
          lambda db: one(db, "SELECT satisfied_at FROM v_gate_term WHERE stage='production-eu' AND freight='F416' AND type='plan_safe_or_approved'")["satisfied_at"]
          == one(db, "SELECT ts FROM v_plan WHERE stage='production-eu' AND freight='F416'")["ts"])
    check("gate: passes is never NULL", A,
          lambda db: q(db, "SELECT * FROM v_gate WHERE passes IS NULL") == [])
    check("uptake: AMI edge pending in US (v13 > v12), not in EU (v9 == v9); image edge auto-taken by policy (v72)", A,
          lambda db: one(db, "SELECT pending FROM v_pending_uptake WHERE consumer='workflow-pool@production' AND key='ami_id'")["pending"] == 1
          and one(db, "SELECT pending FROM v_pending_uptake WHERE consumer='workflow-pool@production-eu'")["pending"] == 0
          and (lambda r: r["pending"] == 0 and r["consumed_version"] == 72 and r["consumed_by"] == "policy:uptake")
          (one(db, "SELECT * FROM v_pending_uptake WHERE key='deploy_image_reference'")))
    check("trace: #46181 (F419) reached testing via F420 only; in no release", A,
          lambda db: (lambda r: r["furthest_stage"] in ("testing", "testing-eu") and r["furthest_via"] == "F420" and r["shipped_in"] is None)
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=46181"))
          and q(db, "SELECT * FROM v_trace_cell WHERE pr=46181 AND reached_at IS NOT NULL AND stage NOT IN ('testing','testing-eu')") == [])
    check("trace: #46173 (F418) is in staging, next is production approval", A,
          lambda db: (lambda r: r["furthest_stage"] == "staging" and r["next"] == "production: approval: oncall")
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=46173")))
    check("trace: #46120 (F417) shipped in PR 14876 to production-eu, nothing next; #46101 shipped in 14870 not a later train", A,
          lambda db: (lambda r: r["furthest_stage"] == "production-eu" and r["next"] is None and r["shipped_in"] == 14876)
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=46120"))
          and one(db, "SELECT shipped_in FROM v_trace_summary WHERE pr=46101")["shipped_in"] == 14870)
    check("observed: production has exactly one mismatched service (api@F416); production-eu matches", A,
          lambda db: (lambda r: r["mismatches"] == 1 and r["mismatch_detail"] == "api@F416")(one(db, "SELECT * FROM v_observed WHERE stage='production'"))
          and one(db, "SELECT mismatches FROM v_observed WHERE stage='production-eu'")["mismatches"] == 0)
    check("releases: three cut, newest first; F418 not in production; F417 and F418 each ship 3 PRs", A,
          lambda db: [r["freight"] for r in q(db, "SELECT freight FROM v_releases")] == ["F418", "F417", "F416"]
          and one(db, "SELECT production_at FROM v_releases WHERE freight='F418'")["production_at"] is None
          and one(db, "SELECT prs FROM v_releases WHERE freight='F417'")["prs"] == 3
          and one(db, "SELECT prs FROM v_releases WHERE freight='F418'")["prs"] == 3)
    check("audit: no decision in the fixture is flagged", A,
          lambda db: q(db, "SELECT * FROM v_audit_flag WHERE flag IS NOT NULL") == [])

    # --- time travel ----------------------------------------------------------
    B = "2026-08-24T16:20:00Z"
    check("as of Mon 16:20: production in-flight, phase both-live, carried still F416", B,
          lambda db: (lambda r: r["status"] == "in-flight" and r["last_phase"] == "both-live" and r["carried"] == "F416")(grid(db, "production")))
    check("as of Mon 16:20: no break-glass, no drift, nothing held; F418 does not exist", B,
          lambda db: q(db, "SELECT stage FROM v_grid WHERE drift IS NOT NULL OR hold_until IS NOT NULL") == []
          and q(db, "SELECT * FROM v_freight WHERE freight='F418'") == [])
    C = "2026-08-24T16:50:00Z"
    check("as of Mon 16:50: production-eu awaiting approval because the plan is not safe; production converged", C,
          lambda db: (lambda r: r["status"] == "awaiting" and r["awaiting"] == "approval: oncall (plan not safe)" and r["candidate"] == "F417"
                      and r["awaiting_since"] == "2026-08-24T16:45:00Z")(grid(db, "production-eu"))
          and (lambda r: r["status"] == "converged" and r["carried"] == "F417" and r["drift"] is None)(grid(db, "production")))
    G = "2026-08-24T16:44:30Z"
    check("as of Mon 16:44:30: production-eu awaiting its plan (passes 0, not NULL)", G,
          lambda db: (lambda r: r["status"] == "awaiting" and r["awaiting"] == "plan for production-eu" and r["passes"] == 0)(grid(db, "production-eu")))
    D = "2026-08-26T15:00:30Z"
    check("as of Wed 15:00:30: staging ready thirty seconds after the cut; production's candidate still F417", D,
          lambda db: (lambda r: r["status"] == "ready" and r["candidate"] == "F418" and r["desired"] == "F417")(grid(db, "staging"))
          and grid(db, "production")["candidate"] == "F417")
    H = "2026-08-18T15:00:30Z"
    check("as of Aug 18 15:00:30: staging has no decision yet and still gets a state (ready)", H,
          lambda db: (lambda r: r["desired"] is None and r["status"] == "ready" and r["candidate"] == "F416")(grid(db, "staging")))
    E = "2026-08-26T16:15:00Z"
    check("as of Wed 16:15: testing-eu failed (circuit breaker), carried still F418; testing in flight with F419", E,
          lambda db: (lambda r: r["status"] == "failed" and "circuit breaker" in (r["last_error"] or "") and r["carried"] == "F418" and r["desired"] == "F419")(grid(db, "testing-eu"))
          and (lambda r: r["status"] == "in-flight" and r["inflight_freight"] == "F419")(grid(db, "testing")))
    F = "2026-08-26T16:21:10Z"
    check("as of Wed 16:21:10: testing superseded (F419 in flight, F420 desired); testing-eu pending", F,
          lambda db: (lambda r: r["status"] == "superseded" and r["inflight_freight"] == "F419" and r["desired"] == "F420")(grid(db, "testing"))
          and grid(db, "testing-eu")["status"] == "pending")

    # --- every policy-written decision passed its gate at its own instant ------
    for f in facts:
        if f["kind"] == "promotion.decided" and f["actor"].startswith("policy:"):
            stage, freight = f["subject"][6:], f["payload"]["freight"]
            check(f"policy decision {f['id']} ({stage} ← {freight}) passed its gate when written", f["ts"],
                  (lambda s, fr: lambda db: one(db, "SELECT passes FROM v_gate WHERE stage=? AND freight=?", s, fr)["passes"] == 1)(stage, freight))

    # --- mutations: break one mechanism, expect the views to say so -------------
    def m_verify_before_deploy(fs):
        v = find(fs, kind="verification.recorded", subject="stage:staging", p_freight="F418", p_check="integration-tests")
        v["ts"] = "2026-08-26T15:10:00Z"
    check("mutation: a verification recorded before staging carried F418 does not satisfy production's gate", A,
          lambda db: grid(db, "production")["awaiting"].startswith("verification: integration-tests in staging (recorded before"),
          m_verify_before_deploy)

    def m_wrong_role(fs):
        fs.append({"id": "m001", "ts": "2026-08-26T17:40:00Z", "class": "intent", "kind": "approval.granted", "subject": "stage:production",
                   "actor": "user:dev1", "payload": {"freight": "F418", "role": "qa", "via": "console"}, "rationale": "looks fine"})
    check("mutation: an approval by the wrong role does not open production", A,
          lambda db: grid(db, "production")["awaiting"] == "approval: oncall", m_wrong_role)

    def m_rogue_decision(fs):
        fs.append({"id": "m002", "ts": "2026-08-26T17:40:00Z", "class": "intent", "kind": "promotion.decided", "subject": "stage:production",
                   "actor": "user:jonas", "payload": {"freight": "F418"}, "rationale": "just ship it"})
    check("mutation: a decision written by a person on a gated stage is flagged by the audit", A,
          lambda db: (lambda r: r is not None and r["flag"].startswith("decided by user:jonas"))(one(db, "SELECT * FROM v_audit_flag WHERE fact='m002'"))
          and grid(db, "production")["status"] == "pending", m_rogue_decision)

    def m_unevidenced_decision(fs):
        a = find(fs, kind="approval.granted", subject="stage:production", p_freight="F417")
        d = find(fs, kind="promotion.decided", subject="stage:production", p_freight="F417")
        d["refs"] = [r for r in d.get("refs", []) if r != a["id"]]
        fs.remove(a)
    check("mutation: a gated decision with no approval on record is flagged by the audit", A,
          lambda db: (lambda r: r["flag"] == "gated stage decided with no oncall approval on record")
          (one(db, "SELECT * FROM v_audit_flag WHERE stage='production' AND freight='F417'")), m_unevidenced_decision)

    def m_second_hold(fs):
        fs.append({"id": "m003", "ts": "2026-08-26T15:00:00Z", "class": "intent", "kind": "hold.placed", "subject": "stage:production-eu",
                   "actor": "user:sam", "payload": {"until": "2026-08-26T22:00:00Z", "scope": "promotions"}, "rationale": "customer freeze"})
    check("mutation: a second active hold keeps one grid row and extends the hold to the later expiry", A,
          lambda db: len(q(db, "SELECT * FROM v_grid")) == 5
          and (lambda r: r["hold_until"] == "2026-08-26T22:00:00Z" and r["holds"] == 2)(grid(db, "production-eu")), m_second_hold)

    def m_duplicate_approval(fs):
        fs.append({"id": "m004", "ts": "2026-08-24T16:05:00Z", "class": "intent", "kind": "approval.granted", "subject": "stage:production",
                   "actor": "user:sam", "payload": {"freight": "F417", "role": "oncall", "via": "console"}, "rationale": "also fine"})
    check("mutation: two approvals at the same instant still yield one row per stage and one release card", A,
          lambda db: len(q(db, "SELECT * FROM v_grid")) == 5 and len(q(db, "SELECT * FROM v_releases WHERE freight='F417'")) == 1
          and len(q(db, "SELECT * FROM v_gate_term WHERE stage='production' AND freight='F417' AND type='approved'")) == 1, m_duplicate_approval)

    def m_no_breakglass(fs):
        fs.remove(find(fs, kind="breakglass.recorded"))
    check("mutation: without the break-glass fact, production's drift renders as UNEXPLAINED", A,
          lambda db: grid(db, "production")["drift"].endswith("UNEXPLAINED"), m_no_breakglass)

    def m_failed_verification(fs):
        find(fs, kind="verification.recorded", subject="stage:staging", p_freight="F418", p_check="integration-tests")["payload"]["outcome"] = "fail"
    check("mutation: a failed staging verification moves production's blocker from approval to verification", A,
          lambda db: grid(db, "production")["awaiting"] == "verification: integration-tests in staging", m_failed_verification)

    # --- run ---------------------------------------------------------------------
    failures, dbs = [], {}
    for name, as_of, mutation, fn in checks:
        if mutation is None:
            db = dbs.get(as_of) or dbs.setdefault(as_of, build_db(facts, as_of))
        else:
            db = build_db(mutate(facts, mutation), as_of)
        try:
            ok = bool(fn(db))
        except Exception as e:  # a broken view is a failed check, with the reason
            ok, name = False, f"{name} — raised {type(e).__name__}: {e}"
        if not ok:
            failures.append(name)
    return len(checks), failures


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(ts):
    if not ts:
        return ""
    return parse_ts(ts).strftime("%a %b %-d %H:%M")


def fmt_since(ts, as_of):
    if not ts:
        return ""
    secs = int((parse_ts(as_of) - parse_ts(ts)).total_seconds())
    if secs < 0:
        return "in the future"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def esc(v):
    return html.escape("" if v is None else str(v))


def mono(v):
    return f'<code>{esc(v)}</code>' if v not in (None, "") else ""


def fact_ref(fid):
    if not fid:
        return ""
    return " ".join(f'<a class="fact" href="#{esc(x)}">{esc(x)}</a>' for x in str(fid).split())


GLYPH = {
    "converged": ("●", "ok"), "awaiting": ("○", "amber"), "held": ("⊘", "hold"),
    "in-flight": ("◌", "live"), "failed": ("✕", "bad"), "ready": ("▸", "ok"),
    "pending": ("○", "hold"), "superseded": ("↷", "live"), "idle": ("·", "hold"),
}


def chip(status, text=None, cls=None):
    g, c = GLYPH.get(status, ("·", "hold"))
    return f'<span class="chip {cls or c}"><span class="glyph">{g}</span>{esc(text or status)}</span>'


def class_chip(cls):
    return f'<span class="cls {esc(cls)}">{esc(cls)}</span>'


def table(cols, rows, cls=""):
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    if not rows:
        body = f'<tr><td colspan="{len(cols)}" class="empty">nothing — the ledger has no rows for this at this instant</td></tr>'
    return f'<div class="scroll"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def sql_block(db, *names, extra=None):
    parts = []
    for n in names:
        r = one(db, "SELECT sql FROM sqlite_master WHERE name=?", n)
        if r:
            parts.append(r["sql"] + ";")
    if extra:
        parts.append(extra.strip())
    return ('<details class="sql"><summary>the query</summary><pre>'
            + esc("\n\n".join(parts)) + "</pre></details>")


def section(anchor, eyebrow, title, blurb, body, sql):
    return (f'<section id="{anchor}"><div class="eyebrow">{esc(eyebrow)}</div>'
            f'<h2>{title}</h2><p class="blurb">{blurb}</p>{body}{sql}</section>')


# ---------------------------------------------------------------------------
# Screens — each is driven by a query; no stage or freight is named in code
# ---------------------------------------------------------------------------

def screen_grid(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_grid"):
        st = r["status"]
        detail = ""
        if st == "awaiting":
            detail = f'{esc(r["awaiting"])} · <b>{esc(fmt_since(r["awaiting_since"], as_of))}</b> (since {esc(fmt_ts(r["awaiting_since"]))})'
        elif st == "held":
            detail = f'{esc(r["awaiting"])} · candidate {mono(r["candidate"])}'
        elif st == "in-flight":
            detail = f'{mono(r["inflight"])} · {mono(r["inflight_freight"])}' + (f' · phase <b>{esc(r["last_phase"])}</b>' if r["last_phase"] else "") + f' · since {esc(fmt_ts(r["inflight_since"]))}'
        elif st == "superseded":
            detail = f'{mono(r["inflight"])} still enacting {mono(r["inflight_freight"])}; decision moved to {mono(r["desired"])} at {esc(fmt_ts(r["decided_at"]))}'
        elif st == "failed":
            detail = (f'{mono(r["last_transition"])} · {esc(fmt_ts(r["last_outcome_at"]))}<br>{esc(r["last_error"])}'
                      + (f' · <span class="muted">{esc(r["failed_step"])} → {esc(r["step_url"])}</span>' if r["failed_step"] else ""))
        elif st == "ready":
            detail = f'gate passes for {mono(r["candidate"])}; enactment not started'
        elif st == "pending":
            detail = f'decided {esc(fmt_ts(r["decided_at"]))}; enactment not started'
        hold = ""
        if r["hold_until"]:
            hold = (f'<div class="note">{chip("held", "hold")} until {esc(fmt_ts(r["hold_until"]))} by {mono(r["hold_by"])} — {esc(r["hold_rationale"])}</div>')
        drift = f'<div class="note drift"><span class="glyph">⚠</span> observed {esc(r["drift"])} · read {esc(fmt_ts(r["observed_at"]))}</div>' if r["drift"] else ""
        candidate = ""
        if r["candidate"] and r["candidate"] != r["desired"] and st not in ("awaiting", "held", "ready"):
            candidate = f' <span class="muted">→ {esc(r["candidate"])} offered</span>'
        rows.append([
            f'<b>{esc(r["stage"])}</b><div class="muted small">{esc(r["region"])} · {esc(r["owner"])}</div>',
            chip(st) + (f'<div class="small">{detail}</div>' if detail else "") + hold + drift,
            (f'{mono(r["desired"])}{candidate}<div class="muted small">{esc(fmt_ts(r["decided_at"]))} · {esc(r["decided_by"])}</div>' if r["desired"] else '<span class="muted">no decision yet</span>' + candidate),
            (f'{mono(r["carried"])}<div class="muted small">since {esc(fmt_ts(r["carried_since"]))} · {esc(fmt_since(r["carried_since"], as_of))} · update #{esc(r["ops_update"])}</div>' if r["carried"] else '<span class="muted">nothing yet</span>'),
        ])
    body = table(["stage", "state (derived)", "should carry (intent)", "carries (observed)"], rows, "grid")
    return section("grid", "1 · the subject grid", "What is where, since when, waiting on whom",
                   "One row per stage. <em>Should carry</em> is the latest promotion decision; <em>carries</em> is the "
                   "latest enactment that finished; the state column joins the two with the gate evaluation, active "
                   "holds, in-flight or failed enactments, and the last conformance read. Nothing in it is stored.",
                   body, sql_block(db, "v_grid", "v_gate_eval", "v_candidate"))


def screen_lanes(db, as_of):
    stages = [r["stage"] for r in q(db, "SELECT stage FROM v_stage ORDER BY ord")]
    freights = q(db, "SELECT DISTINCT freight, release_pr, cut_at, discovered_at FROM v_lanes ORDER BY discovered_at DESC")
    lanes = {(r["freight"], r["stage"]): r for r in q(db, "SELECT * FROM v_lanes")}
    rows = []
    for fr in freights:
        label = f'<b>{esc(fr["freight"])}</b>'
        label += (f'<div class="muted small">release PR #{esc(fr["release_pr"])} · cut {esc(fmt_ts(fr["cut_at"]))}</div>' if fr["release_pr"]
                  else f'<div class="muted small">master build · {esc(fmt_ts(fr["discovered_at"]))} · no release yet</div>')
        cells = [label]
        for s in stages:
            c = lanes[(fr["freight"], s)]
            if c["reached_at"]:
                cur = " " + chip("converged", "current", "ok tiny") if c["is_current"] else ""
                cells.append(f'<span class="reached">{esc(fmt_ts(c["reached_at"]))}</span>{cur}')
            elif c["inflight_since"]:
                cells.append(chip("in-flight", "in flight") + f'<div class="small muted">since {esc(fmt_ts(c["inflight_since"]))}</div>')
            elif c["awaiting"]:
                cells.append(chip("awaiting") + f'<div class="small">{esc(c["awaiting"])}<br><span class="muted">{esc(fmt_since(c["awaiting_since"], as_of))}</span></div>')
            elif c["last_outcome"] in ("failed", "abandoned"):
                cells.append(chip("failed" if c["last_outcome"] == "failed" else "superseded", c["last_outcome"]) + f'<div class="small muted">{esc(fmt_ts(c["last_outcome_at"]))}</div>')
            else:
                cells.append('<span class="muted">—</span>')
        rows.append(cells)
    body = table(["freight"] + stages, rows, "lanes")
    return section("lanes", "1 · freight lanes", "Freight × stage",
                   "Each freight runs through the stages it has reached; a cell is the time the enactment finished "
                   "there, or what the freight is waiting on, or how its last attempt ended. Under content keying the "
                   "build that ran in testing would <em>be</em> the release — today pulumi-service rebuilds per stage "
                   "from the git SHA, so this lane is the thesis's construct, not yet the pipeline's.",
                   body, sql_block(db, "v_lanes"))


def screen_gates(db, as_of):
    waiting = q(db, "SELECT * FROM v_grid WHERE status IN ('awaiting','held','ready') ORDER BY ord")
    if not waiting:
        return section("gates", "2 · awaiting ≠ failure", "No gate is waiting right now",
                       "Every stage's candidate is either its current freight or in flight.", "", sql_block(db, "v_gate_term", "v_gate"))
    parts, heads = [], []
    for g in waiting:
        terms = q(db, "SELECT * FROM v_gate_term WHERE stage=? AND freight=? ORDER BY idx", g["stage"], g["candidate"])
        pol = one(db, "SELECT * FROM v_policy WHERE stage=?", g["stage"])
        rows = []
        for t in terms:
            if t["satisfied_at"] is not None:
                rows.append([chip("converged", "met", "ok"), esc(t["label"]),
                             esc(fmt_ts(t["satisfied_at"])) if t["satisfied_at"] else '<span class="muted">—</span>',
                             esc(t["evidence"]) + " " + fact_ref(t["evidence_fact"])])
            else:
                rows.append([chip("awaiting", "open"), esc(t["label"]),
                             f'<span class="muted">{esc(t["unmet_text"])}</span>',
                             (esc(t["evidence"]) + " " + fact_ref(t["evidence_fact"])) if t["evidence"] else "nothing running while it waits"])
        if g["status"] == "ready":
            verdict = f'passes for <code>{esc(g["candidate"])}</code>; the enactment has not started.'
            heads.append(f'{g["stage"]}: gate passes')
        else:
            verdict = (f'does not pass for <code>{esc(g["candidate"])}</code> — awaiting <b>{esc(g["awaiting"])}</b> '
                       f'for <b>{esc(fmt_since(g["awaiting_since"], as_of))}</b> (since {esc(fmt_ts(g["awaiting_since"]))}).')
            heads.append(f'{g["stage"]}: {g["awaiting"]}')
        parts.append(f'<h3>{esc(g["stage"])} <span class="muted">· policy {esc(pol["mode"])}</span></h3>'
                     f'<p class="lede"><code>{esc(pol["rule"])}</code><br>{verdict}</p>' + table(["", "term", "satisfied", "evidence"], rows))
    return section("gates", "2 · awaiting ≠ failure", " · ".join(esc(h) for h in heads),
                   "Gates are queries over both fact classes, evaluated at rest — each term is a row here, with the "
                   "fact that satisfies it or the reason it doesn't. An unmet term makes the stage <em>awaiting</em>: "
                   "an honest open item with a duration, not a failed run. Nothing is running while it waits.",
                   "".join(parts), sql_block(db, "v_gate_term", "v_gate"))


def screen_trace(db, as_of):
    stages = [r["stage"] for r in q(db, "SELECT stage FROM v_stage ORDER BY ord")]
    cells = {(r["pr"], r["stage"]): r for r in q(db, "SELECT * FROM v_trace_cell")}
    rows = []
    for s in q(db, "SELECT * FROM v_trace_summary ORDER BY pr DESC"):
        line = f'<b>#{esc(s["pr"])}</b> {esc(s["title"])}<div class="muted small">{esc(s["author"])} · merged in {esc(s["introduced_in"])}'
        line += f' · shipped in release PR #{esc(s["shipped_in"])}' if s["shipped_in"] else ""
        line += "</div>"
        if s["furthest_stage"]:
            where = f'in <b>{esc(s["furthest_stage"])}</b> since {esc(fmt_ts(s["furthest_at"]))}'
            if s["furthest_via"] != s["introduced_in"]:
                where += f' <span class="muted">(via {esc(s["furthest_via"])})</span>'
        else:
            where = "nowhere yet"
        if s["next"]:
            where += f'<div class="small amber-text">next — {esc(s["next"])}</div>'
        if s["note"]:
            where += f'<div class="small muted">{esc(s["note"])}</div>'
        row = [line, where]
        for st in stages:
            c = cells[(s["pr"], st)]
            if c["reached_at"]:
                row.append(f'<span class="reached">{esc(fmt_ts(c["reached_at"]))}</span>' + (f'<div class="small muted">{esc(c["via"])}</div>' if c["via"] != s["introduced_in"] else ""))
            elif c["inflight_since"]:
                row.append(chip("in-flight", "in flight"))
            elif c["awaiting"]:
                row.append(chip("awaiting"))
            elif c["last_outcome"] in ("failed", "abandoned"):
                row.append(chip("failed" if c["last_outcome"] == "failed" else "superseded", c["last_outcome"]))
            else:
                row.append('<span class="muted">—</span>')
        rows.append(row)
    body = table(["change", "where is it", *stages], rows, "trace")
    return section("trace", "3 · where is my change", "PR → freight → stages",
                   "Keith's tracker reconstructs this by mining CI logs and commit subjects. Here it is a join: a "
                   "freight names the PRs it introduced, membership is cumulative along master, the lanes say where "
                   "each freight is. A PR merged after the cut is not <em>unreleased</em> — it is in testing, with a "
                   "timestamp, because testing is a stage.",
                   body, sql_block(db, "v_membership", "v_trace_cell", "v_trace_summary"))


def screen_diffgate(db, as_of):
    stages = q(db, "SELECT DISTINCT stage FROM v_policy_term WHERE type='plan_safe_or_approved'")
    if not stages:
        return ""
    parts, heads = [], []
    for s in stages:
        st = s["stage"]
        pol = one(db, "SELECT * FROM v_policy WHERE stage=?", st)
        rows = []
        for r in q(db, "SELECT g.*, t.satisfied_at AS term_at, t.evidence AS term_evidence, t.evidence_fact AS term_fact, t.unmet_text AS term_unmet, "
                       "pl.n_create, pl.n_update, pl.n_delete, pl.n_replace, pl.migrations, pl.migrations_changed, pl.safe, pl.against, pl.fact AS plan_fact "
                       "FROM v_gate g JOIN v_gate_term t ON t.stage=g.stage AND t.freight=g.freight AND t.type='plan_safe_or_approved' "
                       "LEFT JOIN v_plan pl ON pl.stage=g.stage AND pl.freight=g.freight WHERE g.stage=? ORDER BY g.freight", st):
            if r["plan_fact"] is None:
                plan = '<span class="muted">no plan yet</span>'
            else:
                plan = (f'+{esc(r["n_create"])} ~{esc(r["n_update"])} −{esc(r["n_delete"])} ±{esc(r["n_replace"])} {fact_ref(r["plan_fact"])}'
                        f'<div class="small muted">against {esc(r["against"])}</div>')
                if r["migrations_changed"]:
                    plan += f'<div class="small"><span class="glyph">⚠</span> migrations: {esc(", ".join(json.loads(r["migrations"]) if r["migrations"] else []))}</div>'
            if r["term_at"] is not None and r["safe"] == 1:
                term = chip("ready", "auto") + '<div class="small">safe plan — no approval needed</div>'
            elif r["term_at"] is not None:
                term = chip("converged", "approved") + f'<div class="small">not safe → oncall approved: {esc(r["term_evidence"])} {fact_ref(r["term_fact"])}</div>'
            else:
                term = chip("awaiting", "open") + f'<div class="small">{esc(r["term_unmet"])}</div>'
            overall = (chip("ready", "gate passes") if r["passes"] else chip("awaiting", "gate: " + r["awaiting"]))
            rows.append([mono(r["freight"]), plan, term, overall])
        heads.append(st)
        parts.append(f'<h3>{esc(st)}</h3><p class="lede"><code>{esc(pol["rule"])}</code><br>where <em>safe</em> = <code>{esc(pol["safe_rule"])}</code></p>'
                     + table(["freight", "plan", "plan-safe-or-approved term", "whole gate"], rows))
    return section("diffgate", "4 · a diff-gate", "\"If migrations changed, require approval; otherwise fast-track\"",
                   "Joe's question, answered as a query: the plan is a fact (stage 3 of the key decomposition — "
                   "desired × world at T), so a gate term can read it. Same rule, every freight, each outcome read "
                   "from the ledger — including freights that are not candidates yet, because a plan can be computed "
                   "ahead and the term evaluated at rest. The whole-gate column shows what else the stage waits on.",
                   "".join(parts), sql_block(db, "v_plan"))


def screen_uptake(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_pending_uptake ORDER BY consumer, key"):
        if r["pending"] is None:
            state = '<span class="muted">nothing published yet</span>'
        elif r["pending"]:
            state = (chip("awaiting", f'v{r["published_version"]} available, {r["policy"]}')
                     + f'<div class="small">since {esc(fmt_ts(r["published_at"]))} · {esc(fmt_since(r["published_at"], as_of))} · '
                       f'<a href="#" class="preview">{esc(r["preview"])}</a></div>')
        else:
            who = "by policy" if str(r["consumed_by"]).startswith("policy:") else f'by {r["consumed_by"]}'
            state = chip("converged", f'current at v{r["consumed_version"]}') + f'<div class="small muted">taken up {who} · {esc(fmt_ts(r["consumed_at"]))}</div>'
        rows.append([
            f'<b>{esc(r["consumer"])}</b><div class="small muted">{esc(r["key"])} ← {esc(r["producer"])} · uptake <b>{esc(r["policy"])}</b></div>',
            (f'v{esc(r["published_version"])} {mono(r["published_value"])}<div class="small muted">published {esc(fmt_ts(r["published_at"]))} {fact_ref(r["published_fact"])}</div>' if r["published_version"] is not None else '<span class="muted">—</span>'),
            (f'v{esc(r["consumed_version"])}<div class="small muted">{esc(fmt_ts(r["consumed_at"]))} · {esc(r["consumed_by"])}</div>' if r["consumed_version"] is not None else '<span class="muted">never</span>'),
            state + f'<div class="small muted">{esc(r["description"])}</div>',
        ])
    body = table(["consumer · binding", "published (evidence)", "taken up (intent)", "state"], rows)
    return section("uptake", "5 · uptake, gated and auto", "Publication is evidence; uptake is intent",
                   "Each binding declares its uptake policy. The AMI bake published a new record and the worker pool's "
                   "binding is gated, so the grid shows <em>available, gated</em> with a preview instead of an instance "
                   "refresh nobody decided on. The image-reference binding is auto: the policy wrote the uptake decision "
                   "the moment production published. Blast radius is <code>consumers WHERE pending</code>.",
                   body, sql_block(db, "v_pending_uptake", "v_record", "v_uptaken"))


def screen_outofband(db, as_of):
    reads = q(db, "SELECT o.*, s.ord FROM v_observed o JOIN v_stage s ON s.stage=o.stage ORDER BY (o.mismatches > 0) DESC, s.ord")
    if not reads:
        return section("oob", "the side door", "Out-of-band reality", "No conformance reads yet.", "", sql_block(db, "v_observed"))
    parts, heads = [], []
    for o in reads:
        bg = one(db, "SELECT bg.* FROM v_breakglass bg LEFT JOIN v_carried k ON k.stage=bg.stage WHERE bg.stage=? AND bg.ts > coalesce(k.since,'')", o["stage"])
        rows = []
        for svc in q(db, "SELECT * FROM v_observed_service WHERE stage=? ORDER BY service", o["stage"]):
            rows.append([mono(svc["service"]), mono(svc["desired"]), mono(svc["observed"]),
                         chip("converged", "matches") if svc["matches"] else chip("failed", "differs", "bad")])
        body = (f'<h3>{esc(o["stage"])} <span class="muted">· read {esc(fmt_ts(o["ts"]))} · {esc(o["source"])} {fact_ref(o["fact"])}</span></h3>'
                + table(["service", "should run", "runs", ""], rows))
        if o["mismatches"] and bg:
            heads.append(f'{o["stage"]}: drift explained by {bg["incident"]}')
            body += (f'<div class="callout"><span class="cls intent">intent</span> <b>break-glass</b> {fact_ref(bg["fact"])} · '
                     f'{esc(fmt_ts(bg["ts"]))} · {esc(bg["actor"])} · {esc(bg["incident"])}<br>'
                     f'<em>{esc(bg["action"])}</em> on <code>{esc(bg["scope"])}</code>, {esc(bg["from_freight"])} → {esc(bg["to_freight"])}; '
                     f'expires {esc(bg["expiry"])}.<br><span class="muted">{esc(bg["rationale"])}</span></div>')
        elif o["mismatches"]:
            heads.append(f'{o["stage"]}: drift UNEXPLAINED')
            body += '<div class="callout bad">Observed state differs from intent and no side-door fact explains it.</div>'
        else:
            heads.append(f'{o["stage"]}: matches')
        parts.append(body)
    return section("oob", "the side door", " · ".join(esc(h) for h in heads),
                   "A conformance watch reads what is running and the ledger compares it to intent. When someone acts "
                   "out of band and records it, the side door writes the same class of fact — with the same attribution "
                   "— as a front-door promotion, so automation can respect it instead of undoing it, and adopt-vs-revert "
                   "is decidable. Drift with no such fact renders as <em>UNEXPLAINED</em>.",
                   "".join(parts), sql_block(db, "v_observed_service", "v_breakglass"))


def screen_releases(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_releases"):
        rows.append([
            f'<b>{esc(r["freight"])}</b><div class="small muted">PR #{esc(r["release_pr"])} · {esc(r["release_branch"])} · {esc(r["sha"])}</div>'
            f'<div class="small muted">{esc(r["prs"])} PRs: {esc(r["pr_list"])}</div>',
            esc(fmt_ts(r["cut_at"])), esc(fmt_ts(r["staging_at"])) or '<span class="muted">—</span>',
            (f'{esc(r["approved_by"])}<div class="small muted">{esc(fmt_ts(r["approved_at"]))}</div>' if r["approved_at"] else chip("awaiting")),
            esc(fmt_ts(r["production_at"])) or '<span class="muted">—</span>',
            esc(fmt_ts(r["production_eu_at"])) or '<span class="muted">—</span>',
        ])
    body = table(["release", "cut", "staging", "approved", "production", "production-eu"], rows)
    return section("releases", "Keith parity", "Release trains",
                   "The past-releases cards: cut, staged, approved by whom, live where, shipping which PRs (those "
                   "introduced since the previous cut). One query over lanes, approvals and membership.",
                   body, sql_block(db, "v_releases", "v_release_prs"))


def screen_transitions(db, as_of):
    out = []
    ts_ = q(db, "SELECT * FROM v_transition WHERE stack='service' AND (last_phase IS NOT NULL OR resource_steps > 0 OR outcome IN ('failed','abandoned')) ORDER BY started_at")
    for t in ts_:
        facts = q(db, "SELECT * FROM facts WHERE subject=? ORDER BY seq", t["transition"])
        rows = []
        for f in facts:
            p = json.loads(f["payload"])
            what = {"transition.started": f'started' + (f' · {esc(p.get("strategy"))}' if p.get("strategy") else ""),
                    "transition.phase": f'phase <b>{esc(p.get("phase"))}</b> — {esc(p.get("detail"))}',
                    "resource.step": f'{esc(p.get("op"))} {mono(p.get("type"))}<div class="small muted">{esc(p.get("urn"))}</div>' + (f'<div class="small bad-text">{esc(p.get("error"))}</div>' if p.get("error") else ""),
                    "transition.finished": f'finished · <b>{esc(p.get("outcome"))}</b>' + (f' · update #{esc(p.get("ops_update"))}' if p.get("ops_update") else "") + (f' · {esc(json.dumps(p.get("summary")))}' if p.get("summary") else "") + (f'<div class="small">{esc(p.get("error") or p.get("detail"))}</div>' if p.get("error") or p.get("detail") else ""),
                    }.get(f["kind"], esc(f["kind"]))
            rows.append([esc(fmt_ts(f["ts"])), class_chip(f["class"]), mono(f["kind"]), what, fact_ref(f["id"])])
        jobs = q(db, "SELECT * FROM v_job WHERE stage=? AND freight=? ORDER BY ts", t["stage"], t["freight"])
        for j in jobs:
            what = f'side job <b>{esc(j["job"])}</b> · {esc(j["outcome"])}' + (' · <span class="muted">optional — does not affect the stage</span>' if j["optional"] else "") + f'<div class="small muted">{esc(j["detail"])}</div>'
            rows.append([esc(fmt_ts(j["ts"])), class_chip("observation"), mono("job.finished"), what, fact_ref(j["fact"])])
        rows.sort(key=lambda r: r[0])
        summary = (f'<h3>{mono(t["transition"])} — {esc(t["freight"])} → {esc(t["stage"])} · {chip({"succeeded": "converged", "failed": "failed", "abandoned": "superseded"}.get(t["outcome"], "in-flight"), t["outcome"] or "in flight")}</h3>'
                   f'<p class="small muted">{esc(fmt_ts(t["started_at"]))} → {esc(fmt_ts(t["finished_at"])) or "in flight"} · '
                   f'{esc(t["resource_steps"])} resource steps recorded · run {esc(t["run"])}</p>')
        out.append(summary + table(["when", "class", "kind", "", "fact"], rows))
    return section("transitions", "fact grain ≠ step grain", "Inside a transition",
                   "A transition is the enactment reified: one subject, many facts. Position lives in phase facts the "
                   "grid can read; resource steps land on the same subject when the executor emits them; a failure "
                   "carries its step and error; an abandonment cites the decision that superseded it. Side jobs attach "
                   "to the stage and never gate anything.",
                   "".join(out) or '<p class="muted">No transition with phases, steps or a non-success outcome at this instant.</p>',
                   sql_block(db, "v_transition", "v_job"))


def screen_audit(db, as_of):
    rows = []
    for d in q(db, "SELECT * FROM v_audit_flag ORDER BY (flag IS NULL), ts DESC"):
        rows.append([esc(fmt_ts(d["ts"])), f'<b>{esc(d["stage"])}</b> ← {mono(d["freight"])}', esc(d["actor"]),
                     (esc(d["required_role"]) + " " + (fact_ref(d["approval_fact"]) if d["approval_fact"] else '<span class="bad-text">none</span>')) if d["required_role"] else '<span class="muted">not required</span>',
                     esc(d["n_refs"]),
                     (f'<span class="chip bad"><span class="glyph">⚠</span>{esc(d["flag"])}</span>' if d["flag"] else chip("converged", "clean")),
                     fact_ref(d["fact"])])
    n_flag = one(db, "SELECT count(*) AS n FROM v_audit_flag WHERE flag IS NOT NULL")["n"]
    return section("audit", "the ledger records what it is told", f'Decision audit — {n_flag} flagged',
                   "Every promotion decision, checked against the policy that should have written it: was it written "
                   "by the stage's policy, was the required approval on record when it was written, did it cite "
                   "evidence? An unauthorised or unevidenced decision is still a fact — this is how it stays "
                   "distinguishable from a legitimate one.",
                   table(["when", "decision", "written by", "approval", "refs", "audit", "fact"], rows), sql_block(db, "v_audit_decision", "v_audit_flag"))


def screen_ledger(db, as_of):
    rows = []
    for f in q(db, "SELECT * FROM facts ORDER BY seq DESC LIMIT 14"):
        rows.append([f'<span id="{esc(f["id"])}"></span>{mono(f["id"])}', esc(fmt_ts(f["ts"])), class_chip(f["class"]),
                     mono(f["kind"]), mono(f["subject"]), esc(f["actor"]),
                     f'<div class="payload">{esc(f["payload"])}</div>' + (f'<div class="small"><em>{esc(f["rationale"])}</em></div>' if f["rationale"] else "")])
    total = one(db, "SELECT count(*) AS n FROM facts")["n"]
    body = (f'<p class="lede">{total} facts at this instant; the newest 14 below. Two classes, append-only, every row attributed. '
            'Every screen above is a SELECT over this.</p>' + table(["id", "when", "class", "kind", "subject", "actor", "payload · rationale"], rows, "ledger"))
    return section("ledger", "the record is the interface", "The ledger", "", body,
                   '<details class="sql"><summary>the table</summary><pre>' + esc(open(os.path.join(HERE, "schema.sql")).read()) + "</pre></details>")


SCREENS = [screen_grid, screen_lanes, screen_gates, screen_trace, screen_diffgate, screen_uptake,
           screen_outofband, screen_releases, screen_transitions, screen_audit, screen_ledger]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = """
:root {
  --ground: #f5f6f2; --paper: #ffffff; --ink: #1c2126; --muted: #5f6a73; --rule: #d8ddd5; --rule-soft: #e8ebe4;
  --accent: #1f6a86; --accent-ink: #ffffff; --accent-tint: #e3eff4;
  --ok: #2b7a4b; --ok-tint: #e2f1e6; --amber: #9a6a12; --amber-tint: #f7ecd2; --bad: #a8412a; --bad-tint: #f7e3dd;
  --hold: #66707a; --hold-tint: #e9ecee; --live: #4b5fb5; --live-tint: #e5e9f7; --obs-tint: #eef0eb;
  --code: #2a3138;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #15191c; --paper: #1d2226; --ink: #e4e8e2; --muted: #98a3ab; --rule: #2d353b; --rule-soft: #262d32;
    --accent: #6fb6d0; --accent-ink: #0f1d24; --accent-tint: #1b2f38;
    --ok: #74c58f; --ok-tint: #1c3325; --amber: #e2b04a; --amber-tint: #3a2e14; --bad: #ea8a70; --bad-tint: #3d2219;
    --hold: #9aa5ad; --hold-tint: #2a3238; --live: #9eaee9; --live-tint: #232a44; --obs-tint: #232a2e;
    --code: #d6dbd2;
  }
}
:root[data-theme="dark"] {
  --ground: #15191c; --paper: #1d2226; --ink: #e4e8e2; --muted: #98a3ab; --rule: #2d353b; --rule-soft: #262d32;
  --accent: #6fb6d0; --accent-ink: #0f1d24; --accent-tint: #1b2f38;
  --ok: #74c58f; --ok-tint: #1c3325; --amber: #e2b04a; --amber-tint: #3a2e14; --bad: #ea8a70; --bad-tint: #3d2219;
  --hold: #9aa5ad; --hold-tint: #2a3238; --live: #9eaee9; --live-tint: #232a44; --obs-tint: #232a2e;
  --code: #d6dbd2;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink); font: 15px/1.5 "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; }
a { color: var(--accent); }
code, pre, .mono { font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace; }
code { font-size: 0.92em; color: var(--code); background: var(--obs-tint); padding: 0 .3em; border-radius: 3px; }
h1, h2, h3 { font-family: "Source Serif 4", Georgia, "Times New Roman", serif; font-weight: 600; text-wrap: balance; letter-spacing: -0.01em; }
h1 { font-size: 2.1rem; line-height: 1.15; margin: 0 0 .4rem; }
h2 { font-size: 1.45rem; margin: .15rem 0 .5rem; }
h3 { font-size: 1.05rem; margin: 1.2rem 0 .2rem; }
.page { max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.5rem 5rem; }
header.masthead { display: grid; grid-template-columns: 1fr auto; gap: 1.5rem 3rem; align-items: end; padding-bottom: 1.25rem; border-bottom: 2px solid var(--ink); margin-bottom: 1rem; }
.masthead .kicker { font-size: .78rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .5rem; }
.masthead p.thesis { max-width: 62ch; margin: .4rem 0 0; color: var(--muted); font-size: 1rem; }
.clock { text-align: right; font-variant-numeric: tabular-nums; }
.clock .label { font-size: .78rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
.clock .now { font-family: "IBM Plex Mono", monospace; font-size: 1.05rem; }
nav.snaps { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0 0 2.25rem; }
nav.snaps button { font: inherit; font-size: .86rem; padding: .45rem .8rem; border: 1px solid var(--rule); background: var(--paper); color: var(--ink); border-radius: 4px; cursor: pointer; text-align: left; }
nav.snaps button[aria-pressed="true"] { border-color: var(--accent); background: var(--accent-tint); }
nav.snaps button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
nav.snaps button .t { font-family: "IBM Plex Mono", monospace; display: block; font-size: .8rem; color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: .4rem 1.2rem; font-size: .82rem; color: var(--muted); margin: -1.5rem 0 2rem; }
section { margin: 0 0 3.25rem; }
.eyebrow { font-size: .74rem; letter-spacing: .14em; text-transform: uppercase; color: var(--accent); font-weight: 600; }
p.blurb { max-width: 72ch; color: var(--muted); margin: 0 0 1rem; }
p.lede { max-width: 80ch; margin: 0 0 .9rem; }
.scroll { overflow-x: auto; background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; font-variant-numeric: tabular-nums; }
th { text-align: left; font-weight: 600; font-size: .74rem; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); padding: .6rem .75rem; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { padding: .6rem .75rem; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.empty { color: var(--muted); font-style: italic; }
.small { font-size: .82rem; }
.muted { color: var(--muted); }
.amber-text { color: var(--amber); }
.bad-text { color: var(--bad); }
.reached { white-space: nowrap; }
.chip { display: inline-flex; align-items: center; gap: .35em; padding: .1em .55em .1em .45em; border-radius: 999px; font-size: .8rem; font-weight: 600; white-space: nowrap; line-height: 1.5; }
.chip.tiny { font-size: .7rem; padding: 0 .45em 0 .35em; }
.chip .glyph { font-size: .95em; }
.chip.ok { color: var(--ok); background: var(--ok-tint); }
.chip.amber { color: var(--amber); background: var(--amber-tint); }
.chip.bad { color: var(--bad); background: var(--bad-tint); }
.chip.hold { color: var(--hold); background: var(--hold-tint); }
.chip.live { color: var(--live); background: var(--live-tint); }
.cls { display: inline-block; font-family: "IBM Plex Mono", monospace; font-size: .72rem; letter-spacing: .04em; padding: .05em .45em; border-radius: 3px; }
.cls.intent { color: var(--accent); background: var(--accent-tint); border: 1px solid var(--accent); }
.cls.observation { color: var(--muted); background: var(--obs-tint); border: 1px solid var(--rule); }
.note { margin-top: .35rem; font-size: .82rem; }
.note.drift { color: var(--bad); }
.callout { margin: 1rem 0 .5rem; padding: .8rem 1rem; border-left: 3px solid var(--accent); background: var(--paper); border-radius: 0 6px 6px 0; font-size: .92rem; }
.callout.bad { border-left-color: var(--bad); color: var(--bad); }
a.fact { font-family: "IBM Plex Mono", monospace; font-size: .78rem; text-decoration: none; border-bottom: 1px dotted var(--accent); }
a.preview { font-family: "IBM Plex Mono", monospace; font-size: .8rem; }
.payload { font-family: "IBM Plex Mono", monospace; font-size: .74rem; color: var(--muted); max-width: 46ch; overflow-wrap: anywhere; }
details.sql { margin-top: .6rem; }
details.sql summary { cursor: pointer; font-size: .8rem; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); font-weight: 600; }
details.sql pre { margin: .5rem 0 0; padding: .9rem 1rem; background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; font-size: .78rem; line-height: 1.45; overflow-x: auto; color: var(--code); }
.snapshot[hidden] { display: none; }
footer { border-top: 1px solid var(--rule); padding-top: 1rem; color: var(--muted); font-size: .82rem; max-width: 72ch; }
@media (max-width: 720px) { header.masthead { grid-template-columns: 1fr; } .clock { text-align: left; } }
@media (prefers-reduced-motion: no-preference) { nav.snaps button { transition: border-color .15s ease, background .15s ease; } }
"""

JS = """
(function () {
  var buttons = document.querySelectorAll('nav.snaps button');
  var snaps = document.querySelectorAll('.snapshot');
  var clock = document.querySelector('.clock .now');
  function show(id) {
    snaps.forEach(function (s) { s.hidden = s.dataset.asof !== id; });
    buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.asof === id)); });
    if (clock) clock.textContent = id;
    try { localStorage.setItem('ledger.asof', id); } catch (e) {}
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { show(b.dataset.asof); }); });
  var initial = buttons[0] && buttons[0].dataset.asof;
  try { var saved = localStorage.getItem('ledger.asof'); if (saved && document.querySelector('.snapshot[data-asof="' + saved + '"]')) initial = saved; } catch (e) {}
  if (initial) show(initial);
})();
"""


def render_page(facts, snapshots):
    snap_html, buttons = [], []
    for i, (as_of, caption) in enumerate(snapshots):
        db = build_db(facts, as_of)
        body = "".join(fn(db, as_of) for fn in SCREENS)
        snap_html.append(f'<div class="snapshot" data-asof="{esc(as_of)}"{"" if i == 0 else " hidden"}>{body}</div>')
        buttons.append(f'<button type="button" data-asof="{esc(as_of)}" aria-pressed="{"true" if i == 0 else "false"}">'
                       f'{esc(caption)}<span class="t">{esc(as_of)}</span></button>')
    return f"""<title>Service Release Ledger</title>
<meta name="description" content="pulumi-service's release train, rendered as queries over a two-class fact ledger">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:ital,wght@0,400;0,600;1,400&family=Source+Serif+4:opsz,wght@8..60,600&display=swap">
<style>{CSS}</style>
<div class="page">
<header class="masthead">
  <div>
    <div class="kicker">pulumi-service · delivery ledger · fixture</div>
    <h1>Service Release Ledger</h1>
    <p class="thesis">The release tracker, derived: every screen below is a query over one append-only table of
    attributed facts — <span class="cls intent">intent</span> (decisions, approvals, holds, break-glass) and
    <span class="cls observation">observation</span> (enactments, verifications, plans, conformance reads).
    Nothing is stored as status.</p>
  </div>
  <div class="clock"><div class="label">ledger as of</div><div class="now">{esc(snapshots[0][0])}</div></div>
</header>
<nav class="snaps" aria-label="ledger snapshots">{''.join(buttons)}</nav>
<div class="legend"><span>● converged</span><span>○ awaiting (nothing running)</span><span>◌ in flight</span><span>↷ superseded</span><span>⊘ hold</span><span>⚠ drift</span><span>✕ failed</span><span>fact ids link into the ledger tail</span></div>
{''.join(snap_html)}
<footer>Fixture: the pulumi-service weekday release train (testing on master builds; staging from the release PR;
oncall's merge is production's approval; production-eu as a proposed auto-if-safe follower — today it deploys in
parallel), plus two worker-pool bindings. Generated from a timeline (<code>fixture.py</code>); the views are real.
Source: <code>~/src/nyobe/delivery-ledger</code>.</footer>
</div>
<script>{JS}</script>
"""


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--facts", default=os.path.join(HERE, "facts.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "out", "index.html"))
    ap.add_argument("--as-of", action="append", help="render the ledger as of this instant (repeatable; first is primary)")
    ap.add_argument("--lint-only", action="store_true")
    args = ap.parse_args()

    facts = load_facts(args.facts)
    errors = lint(facts)
    if errors:
        print(f"lint: {len(errors)} problem(s)", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1
    print(f"lint: {len(facts)} facts, clean")

    n, failures = self_checks(facts)
    if failures:
        print(f"self-check: {len(failures)}/{n} failed", file=sys.stderr)
        for f in failures:
            print("  ✗ " + f, file=sys.stderr)
        return 1
    print(f"self-check: {n}/{n} passed")
    if args.lint_only:
        return 0

    snapshots = [(a, a) for a in args.as_of] if args.as_of else SNAPSHOTS
    page = render_page(facts, snapshots)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(page)
    print(f"rendered {args.out} ({len(page)//1024} KB, {len(snapshots)} snapshot(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
