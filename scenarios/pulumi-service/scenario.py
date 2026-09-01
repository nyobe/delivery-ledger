"""pulumi-service's weekday release train — the scenario module.

Page chrome, the snapshot instants, and the self-checks for this fixture.
`render.py` loads this by name (`--scenario pulumi-service`); the facts are in
`facts.jsonl` beside it, emitted by `fixture.py`.
"""

PAGE = {
    "title": "Service Release Ledger",
    "kicker": "pulumi-service · delivery ledger · fixture",
    "description": "pulumi-service's release train, rendered as queries over a two-class fact ledger",
    "thesis": ("The release tracker, derived: every screen below is a query over one append-only table of "
               "attributed facts — <span class=\"cls intent\">intent</span> (decisions, approvals, holds, break-glass) and "
               "<span class=\"cls observation\">observation</span> (enactments, verifications, plans, conformance reads). "
               "Nothing is stored as status."),
    "footer": ("Fixture: the pulumi-service weekday release train (testing on master builds; staging from the release PR; "
               "oncall's merge is production's approval; production-eu as a proposed auto-if-safe follower — today it deploys in "
               "parallel), plus two worker-pool bindings. Generated from a timeline (<code>fixture.py</code>); the views are real."),
}

DEFAULT_AS_OF = "2026-08-26T17:45:00Z"
SNAPSHOTS = [
    # (as_of, caption) — the primary first; the rest are the time-travel tabs.
    # The captions are hand-authored page chrome, the one place this file
    # names freights and stages outside the self-checks; every claim in them
    # is pinned by a self-check at the same instant.
    (DEFAULT_AS_OF,           "Wed 17:45 — F418 waiting on oncall; production drifted since Monday night"),
    ("2026-08-24T16:20:00Z",  "Mon 16:20 — F417 mid-rollout to production, both versions live"),
    ("2026-08-24T16:50:00Z",  "Mon 16:50 — production-eu holds F417 for approval: the plan touches a migration"),
    ("2026-08-26T16:15:00Z",  "Wed 16:15 — F419 failed in testing-eu, still rolling in testing"),
    ("2026-08-26T16:21:10Z",  "Wed 16:21 — F420 supersedes F419 mid-flight in testing"),
]


def self_checks(check, H, facts):
    """Register every check via `check(name, as_of, predicate, mutation=None)`.

    What each view must include AND exclude. A SELECT written against one
    hand-authored fixture passes by construction; these pin the negative
    cases, and the mutation checks break one mechanism at a time to prove
    the views (not the fixture) carry the answer.
    """
    q, one, grid, find = H.q, H.one, H.grid, H.find

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
          and one(db, "SELECT reached_at FROM v_release_stage WHERE freight='F418' AND stage='production'")["reached_at"] is None
          and one(db, "SELECT prs FROM v_releases WHERE freight='F417'")["prs"] == 3
          and one(db, "SELECT prs FROM v_releases WHERE freight='F418'")["prs"] == 3)
    check("releases: approval shown only on stages whose policy has an approval term; F417's EU approval attributed", A,
          lambda db: q(db, "SELECT stage FROM v_release_stage WHERE approvable = 1 GROUP BY stage ORDER BY stage") == [{"stage": "production"}, {"stage": "production-eu"}]
          and one(db, "SELECT approved_by FROM v_release_stage WHERE freight='F417' AND stage='production-eu'")["approved_by"] == "user:maya"
          and one(db, "SELECT approved_by FROM v_release_stage WHERE freight='F416' AND stage='production-eu'")["approved_by"] is None)
    check("lanes: cell classes are view-computed (F419: superseded in testing, failed in testing-eu; F418: awaiting in production)", A,
          lambda db: one(db, "SELECT cell FROM v_lanes WHERE freight='F419' AND stage='testing'")["cell"] == "superseded"
          and one(db, "SELECT cell FROM v_lanes WHERE freight='F419' AND stage='testing-eu'")["cell"] == "failed"
          and one(db, "SELECT cell FROM v_lanes WHERE freight='F418' AND stage='production'")["cell"] == "awaiting"
          and one(db, "SELECT cell FROM v_trace_cell WHERE pr=46181 AND stage='testing'")["cell"] == "reached")
    check("diff-gate: term outcomes are view-computed (F416 auto, F417 approved, F418 open, F420 no-plan)", A,
          lambda db: {r["freight"]: r["term_outcome"] for r in q(db, "SELECT freight, term_outcome FROM v_gate_term WHERE stage='production-eu' AND type='plan_safe_or_approved'")}
          == {"F416": "auto", "F417": "approved", "F418": "open", "F419": "no-plan", "F420": "no-plan"})
    check("audit: no decision in the fixture is flagged; production-eu's two decisions have their approval-bearing term met", A,
          lambda db: q(db, "SELECT * FROM v_audit_flag WHERE flag IS NOT NULL") == []
          and [r["n_unmet"] for r in q(db, "SELECT n_unmet FROM v_audit_decision WHERE stage='production-eu' ORDER BY ts")] == [0, 0]
          and one(db, "SELECT n_required FROM v_audit_decision WHERE stage='production-eu' AND freight='F416'")["n_required"] == 1)

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
          lambda db: (lambda r: r["flag"] == "unmet at decision time: approval: oncall")
          (one(db, "SELECT * FROM v_audit_flag WHERE stage='production' AND freight='F417'")), m_unevidenced_decision)

    def m_unevidenced_eu_decision(fs):
        a = find(fs, kind="approval.granted", subject="stage:production-eu", p_freight="F417")
        d = find(fs, kind="promotion.decided", subject="stage:production-eu", p_freight="F417")
        d["refs"] = [r for r in d.get("refs", []) if r != a["id"]]
        fs.remove(a)
    check("mutation: an auto-if-safe decision on an unsafe plan with no approval is flagged (approval inside the disjunctive term counts)", A,
          lambda db: (lambda r: r["flag"] == "unmet at decision time: safe plan or approval: oncall")
          (one(db, "SELECT * FROM v_audit_flag WHERE stage='production-eu' AND freight='F417'"))
          and one(db, "SELECT flag FROM v_audit_flag WHERE stage='production-eu' AND freight='F416'")["flag"] is None, m_unevidenced_eu_decision)

    def m_second_approval_term(fs):
        p = find(fs, kind="policy.declared", subject="stage:production")
        p["payload"]["terms"].append({"type": "approved", "role": "sre"})
    check("mutation: a second approval term is enforced by the gate AND surfaced by the audit", A,
          lambda db: grid(db, "production")["awaiting"] == "approval: oncall"
          and one(db, "SELECT awaiting FROM v_gate WHERE stage='production' AND freight='F417'")["awaiting"] == "approval: sre"
          and (lambda r: r["flag"] == "unmet at decision time: approval: sre")(one(db, "SELECT * FROM v_audit_flag WHERE stage='production' AND freight='F417'")),
          m_second_approval_term)

    def m_verification_never_carried(fs):
        fs.append({"id": "m005", "ts": "2026-08-26T17:00:00Z", "class": "observation", "kind": "verification.recorded", "subject": "stage:staging",
                   "actor": "ci:gha", "payload": {"freight": "F420", "check": "integration-tests", "outcome": "pass", "detail": "412 tests, 0 failures"}})
    check("mutation: a verification for a freight the stage never carried says so, not 're-run'", A,
          lambda db: one(db, "SELECT unmet_text FROM v_gate_term WHERE stage='production' AND freight='F420' AND chk='integration-tests'")["unmet_text"]
          == "verification: integration-tests in staging (staging has not carried F420)", m_verification_never_carried)

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

