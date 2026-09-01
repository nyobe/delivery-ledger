"""Two programs, no Uber program — the scenario module.

Page chrome, the snapshot instants, and the self-checks for the multistack
fixture. `render.py --scenario multistack` loads this; the facts are in
`facts.jsonl` beside it, emitted by `fixture.py`.
"""

PAGE = {
    "title": "Two-Team Delivery Ledger",
    "kicker": "platform + payments · delivery ledger · fixture",
    "description": "Two delivery programs with declared bindings between them, rendered as queries over one fact ledger",
    "thesis": ("Two teams, two programs, one ledger: the platform program (network + cluster) and the payments program "
               "each promote their own freight through dev → staging → prod. Nothing owns the whole graph — the edges "
               "between them are <span class=\"cls intent\">declared</span> on the consumer, one pattern per program "
               "instantiated per environment, and every cross-team uptake is the consuming team's own decision under "
               "its own policy. The Kubernetes 1.31 upgrade below is a pin-set across both."),
    "footer": ("Fixture: an invented two-team estate (a platform program with network and cluster stacks; a payments "
               "program with one stack) over dev, staging and prod, with a within-team auto edge, a cross-team "
               "by-reference edge whose uptake policy differs per environment, and a cross-team by-version pin that "
               "rides the consumer's train. Generated from a timeline (<code>fixture.py</code>); the views are shared "
               "with the pulumi-service scenario."),
}

DEFAULT_AS_OF = "2026-09-01T15:30:00Z"
SNAPSHOTS = [
    # (as_of, caption) — hand-authored chrome; every claim is pinned by a self-check at the same instant.
    (DEFAULT_AS_OF,           "Tue 15:30 — platform/prod on 1.31; payments/prod holds two decisions for oncall"),
    ("2026-09-01T10:30:00Z",  "Tue 10:30 — the cluster leg failed in platform/staging (quota); network already at P12"),
    ("2026-09-01T11:10:00Z",  "Tue 11:10 — payments/staging: the cluster record's preview is not safe; uptake waits"),
    ("2026-09-01T14:21:15Z",  "Tue 14:21 — platform/prod between legs: network at P12, cluster still P11"),
    ("2026-09-02T09:00:00Z",  "Wed 09:00 — everything converged; k8s 1.31 complete in every environment"),
    ("2026-09-02T11:20:00Z",  "Wed 11:20 — payments/prod both live: A231 stable at 90%, A232 canary at 10%, paused on the step gate"),
    ("2026-09-02T11:45:00Z",  "Wed 11:45 — the canary analysis failed; the rollout auto-aborted, A231 untouched, A232 rolled back before it ever carried"),
]

E_PROD = "edge:payments@payments/prod<-cluster@platform/prod.cluster_endpoint"
E_STG = "edge:payments@payments/staging<-cluster@platform/staging.cluster_endpoint"
E_NET_PROD = "edge:cluster@platform/prod<-network@platform/prod.private_subnet_ids"


def self_checks(check, H, facts):
    """Register every check via `check(name, as_of, predicate, mutation=None)`."""
    q, one, grid, find = H.q, H.one, H.grid, H.find
    A = DEFAULT_AS_OF

    def pu(db, consumer):
        return one(db, "SELECT * FROM v_pending_uptake WHERE consumer=?", consumer)

    # --- the primary instant: the grid, per program ---------------------------
    check("grid: six stages; platform/* converged on P12; payments dev+staging on A231; payments/prod awaiting oncall for A231", A,
          lambda db: len(q(db, "SELECT stage FROM v_grid")) == 6
          and all(grid(db, f"platform/{e}")["status"] == "converged" and grid(db, f"platform/{e}")["carried"] == "P12" for e in ("dev", "staging", "prod"))
          and all(grid(db, f"payments/{e}")["status"] == "converged" and grid(db, f"payments/{e}")["carried"] == "A231" for e in ("dev", "staging"))
          and (lambda r: r["status"] == "awaiting" and r["awaiting"] == "approval: payments-oncall" and r["candidate"] == "A231" and r["desired"] == "A230")
          (grid(db, "payments/prod")))
    check("carried is per (stage, stack): nine stack rows, platform stages carry P12 on both stacks, since = the later leg", A,
          lambda db: len(q(db, "SELECT * FROM v_carried_stack")) == 9
          and grid(db, "platform/prod")["stacks_detail"] == "cluster@P12, network@P12"
          and grid(db, "platform/prod")["carried_since"] == "2026-09-01T14:50:00Z")
    check("carried since is when the freight arrived, not its last re-enactment (payments/staging A231 re-enacted at 11:40 for the record uptake)", A,
          lambda db: grid(db, "payments/staging")["carried_since"] == "2026-09-01T09:17:00Z"
          and one(db, "SELECT last_enacted_at FROM v_carried_stack WHERE stage='payments/staging'")["last_enacted_at"] == "2026-09-01T11:40:00Z"
          and grid(db, "payments/prod")["awaiting_since"] == "2026-09-01T10:05:00Z")
    check("lanes: P12 reached each platform stage when its cluster leg finished, never a payments stage; A231 the mirror", A,
          lambda db: one(db, "SELECT reached_at FROM v_lanes WHERE freight='P12' AND stage='platform/prod'")["reached_at"] == "2026-09-01T14:50:00Z"
          and one(db, "SELECT reached_at FROM v_lanes WHERE freight='P12' AND stage='platform/staging'")["reached_at"] == "2026-09-01T11:05:00Z"
          and q(db, "SELECT * FROM v_lanes WHERE freight='P12' AND stage LIKE 'payments/%' AND cell <> 'none'") == []
          and q(db, "SELECT * FROM v_lanes WHERE freight='A231' AND stage LIKE 'platform/%' AND cell <> 'none'") == []
          and one(db, "SELECT cell FROM v_lanes WHERE freight='A231' AND stage='payments/prod'")["cell"] == "awaiting")
    check("membership is per warehouse: platform PRs are in no payments freight and vice versa; candidates never cross programs", A,
          lambda db: q(db, "SELECT * FROM v_membership WHERE warehouse='payments' AND pr IN (812, 810, 801, 799)") == []
          and q(db, "SELECT * FROM v_membership WHERE warehouse='platform' AND pr IN (4431, 4428, 4410, 4402)") == []
          and {r["stage"]: r["freight"] for r in q(db, "SELECT stage, freight FROM v_candidate")}
              == {"platform/dev": "P12", "platform/staging": "P12", "platform/prod": "P12",
                  "payments/dev": "A231", "payments/staging": "A231", "payments/prod": "A231"})
    check("trace: #812 is in platform/prod; #4428 is in payments/staging, next is prod approval; no release-train note in a trainless program", A,
          lambda db: (lambda r: r["furthest_stage"] == "platform/prod" and r["next"] is None)(one(db, "SELECT * FROM v_trace_summary WHERE pr=812"))
          and (lambda r: r["furthest_stage"] == "payments/staging" and r["next"] == "payments/prod: approval: payments-oncall" and r["note"] is None)
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=4428")))
    check("gate: payments/prod × A231 has both verifications met and only the approval open", A,
          lambda db: [r["type"] for r in q(db, "SELECT type FROM v_gate_term WHERE stage='payments/prod' AND freight='A231' AND satisfied_at IS NULL")] == ["approved"]
          and one(db, "SELECT n_terms FROM v_gate WHERE stage='payments/prod' AND freight='A231'")["n_terms"] == 3)

    # --- uptake: by reference ---------------------------------------------------
    check("uptake: exactly one pending — payments@payments/prod, gated, v4 over v3, gate open on the approval, preview not safe (1 replace)", A,
          lambda db: [r["consumer"] for r in q(db, "SELECT consumer FROM v_pending_uptake WHERE pending = 1")] == ["payments@payments/prod"]
          and (lambda r: r["policy"] == "gated" and r["published_version"] == 4 and r["consumed_version"] == 3 and r["passes"] == 0
               and r["awaiting"] == "approval: payments-oncall" and r["p_replace"] == 1 and r["preview_safe"] == 0
               and r["preview_freight"] == "A230")(pu(db, "payments@payments/prod")))
    check("uptake: the prod edge's not_held term is met and its approval term is the only open one; the 'preview' is a fact, not a string", A,
          lambda db: {r["type"]: r["satisfied_at"] is not None for r in q(db, "SELECT * FROM v_uptake_term WHERE edge=? AND version=4", E_PROD)}
              == {"not_held": True, "approved": False}
          and pu(db, "payments@payments/prod")["preview_fact"] is not None)
    check("uptake: within-team edges are auto and current at v2 by policy; payments/dev current at v4 by policy; staging took v4 by approval (preview not safe)", A,
          lambda db: all(r["pending"] == 0 and r["consumed_version"] == 2 and r["consumed_by"] == "policy:uptake"
                         for r in q(db, "SELECT * FROM v_pending_uptake WHERE consumer LIKE 'cluster@%'"))
          and (lambda r: r["pending"] == 0 and r["consumed_version"] == 4 and r["consumed_by"] == "policy:uptake")(pu(db, "payments@payments/dev"))
          and one(db, "SELECT term_outcome FROM v_uptake_term WHERE edge=? AND version=4 AND type='plan_safe_or_approved'", E_STG)["term_outcome"] == "approved")
    check("uptake: patterns are not instances — no pattern row renders as a pending uptake", A,
          lambda db: q(db, "SELECT * FROM v_pending_uptake WHERE consumer IN ('cluster', 'payments')") == []
          and len(q(db, "SELECT * FROM v_edge WHERE role='pattern'")) == 3
          and len(q(db, "SELECT * FROM v_edge_instance")) == 7)

    # --- uptake: by version -----------------------------------------------------
    check("pin: v41 published, pinned by the bot's PR #4431, first carried by A231; dev and staging current, prod behind on v40 with A231 awaiting", A,
          lambda db: (lambda r: r["pending"] == 0 and r["pinned_version"] == 41 and r["pinned_by"] == "bot:renovate" and r["pinned_pr"] == 4431
                      and r["pinned_in"] == "A231")(one(db, "SELECT * FROM v_pin_uptake"))
          and {r["stage"]: (r["pin_state"], r["carried_pin"]) for r in q(db, "SELECT * FROM v_pin_stage")}
              == {"payments/dev": ("current", 41), "payments/staging": ("current", 41), "payments/prod": ("behind", 40)}
          and one(db, "SELECT cell FROM v_pin_stage WHERE stage='payments/prod'")["cell"] == "awaiting")
    check("as of Tue 08:30: v41 published and not yet pinned — pending on the by-version edge; every stage still on v40", "2026-09-01T08:30:00Z",
          lambda db: one(db, "SELECT pending, pinned_version FROM v_pin_uptake")["pending"] == 1
          and one(db, "SELECT pinned_version FROM v_pin_uptake")["pinned_version"] == 40
          and all(r["pin_state"] == "unpinned" and r["carried_pin"] == 40 for r in q(db, "SELECT * FROM v_pin_stage")))

    # --- the pin-set, the estate, the impact queue --------------------------------
    check("pin-set: k8s 1.31 complete in dev (09:38) and staging (11:05), partial 1/2 in prod", A,
          lambda db: {r["environment"]: (r["state"], r["members_carried"], r["complete_at"]) for r in q(db, "SELECT * FROM v_pinset_status")}
              == {"dev": ("complete", 2, "2026-09-01T09:38:00Z"), "staging": ("complete", 2, "2026-09-01T11:05:00Z"), "prod": ("partial", 1, None)})
    check("estate: payments/prod is wired with cluster_endpoint v3 and has one uptake pending; platform/prod has one pending downstream", A,
          lambda db: (lambda r: r["wired"] == "cluster_endpoint v3" and r["pending_uptakes"] == 1)(one(db, "SELECT * FROM v_estate WHERE stage='payments/prod'"))
          and (lambda r: r["wired"] == "private_subnet_ids v2" and r["pending_downstream"] == 1)(one(db, "SELECT * FROM v_estate WHERE stage='platform/prod'")))
    check("impact: from network@platform/prod the queue reaches payments two hops away, pending on the approval; from platform-images, payments is current by pin", A,
          lambda db: [(r["consumer"], r["depth"], r["pending"]) for r in q(db, "SELECT * FROM v_impact WHERE root='network@platform/prod' ORDER BY depth")]
              == [("cluster@platform/prod", 1, 0), ("payments@payments/prod", 2, 1)]
          and (lambda r: r["kind"] == "by-version" and r["pending"] == 0 and r["pinned_in"] == "A231")(one(db, "SELECT * FROM v_impact WHERE root='platform-images'")))
    check("audit: no promotion and no uptake decision is flagged", A,
          lambda db: q(db, "SELECT * FROM v_audit_flag WHERE flag IS NOT NULL") == []
          and q(db, "SELECT * FROM v_uptake_audit_flag WHERE flag IS NOT NULL") == []
          and one(db, "SELECT n_required FROM v_uptake_audit_flag WHERE edge=? AND version=3", E_PROD)["n_required"] == 1)

    # --- time travel ---------------------------------------------------------------
    B = "2026-09-01T10:30:00Z"
    check("as of Tue 10:30: platform/staging failed on the cluster leg (quota) with network already at P12; lanes cell failed; payments untouched", B,
          lambda db: (lambda r: r["status"] == "failed" and "ResourceLimitExceeded" in r["last_error"] and r["stacks_detail"] == "cluster@P11, network@P12"
                      and r["carried"] is None)(grid(db, "platform/staging"))
          and one(db, "SELECT cell FROM v_lanes WHERE freight='P12' AND stage='platform/staging'")["cell"] == "failed"
          and grid(db, "platform/prod")["carried"] == "P11" and grid(db, "payments/staging")["carried"] == "A231")
    C = "2026-09-01T11:10:00Z"
    check("as of Tue 11:10: the staging edge is pending on v4 — preview not safe, waiting on payments-oncall (term outcome open); staging carries P12 again", C,
          lambda db: (lambda r: r["pending"] == 1 and r["passes"] == 0 and r["awaiting"] == "approval: payments-oncall (preview not safe)")(pu(db, "payments@payments/staging"))
          and one(db, "SELECT term_outcome FROM v_uptake_term WHERE edge=? AND version=4", E_STG)["term_outcome"] == "open"
          and grid(db, "platform/staging")["status"] == "converged" and grid(db, "platform/staging")["carried"] == "P12")
    D = "2026-09-01T14:21:15Z"
    check("as of Tue 14:21:15: platform/prod is partial between legs — network at P12, cluster at P11 — in the grid, the lanes and the estate", D,
          lambda db: (lambda r: r["status"] == "partial" and r["carried"] is None and r["stacks_detail"] == "cluster@P11, network@P12"
                      and r["n_stacks_carrying"] == 2)(grid(db, "platform/prod"))
          and one(db, "SELECT cell, partial_detail FROM v_lanes WHERE freight='P12' AND stage='platform/prod'")["cell"] == "partial"
          and one(db, "SELECT status FROM v_estate WHERE stage='platform/prod'")["status"] == "partial"
          and one(db, "SELECT state FROM v_pinset_status WHERE environment='prod'")["state"] == "pending")
    check("as of Tue 14:23: platform/prod in flight on the cluster leg, phase control-plane", "2026-09-01T14:23:30Z",
          lambda db: (lambda r: r["status"] == "in-flight" and r["last_phase"] == "control-plane" and r["inflight_freight"] == "P12")(grid(db, "platform/prod")))
    E = "2026-09-02T09:00:00Z"
    check("as of Wed 09:00: everything converged, nothing pending, k8s 1.31 complete everywhere (prod at 16:35), payments/prod wired with v4", E,
          lambda db: q(db, "SELECT stage FROM v_grid WHERE status <> 'converged'") == []
          and q(db, "SELECT * FROM v_pending_uptake WHERE pending = 1") == [] and one(db, "SELECT pending FROM v_pin_uptake")["pending"] == 0
          and {r["environment"]: (r["state"], r["complete_at"]) for r in q(db, "SELECT * FROM v_pinset_status")}
              == {"dev": ("complete", "2026-09-01T09:38:00Z"), "staging": ("complete", "2026-09-01T11:05:00Z"), "prod": ("complete", "2026-09-01T16:35:00Z")}
          and one(db, "SELECT wired FROM v_estate WHERE stage='payments/prod'")["wired"] == "cluster_endpoint v4"
          and q(db, "SELECT * FROM v_uptake_audit_flag WHERE flag IS NOT NULL") == [])
    check("as of Tue 13:00: platform/prod × P12 waits only on platform-oncall — both staging verifications count after the retried cluster leg", "2026-09-01T13:00:00Z",
          lambda db: (lambda r: r["status"] == "awaiting" and r["awaiting"] == "approval: platform-oncall" and r["candidate"] == "P12")(grid(db, "platform/prod")))

    check("as of Tue 14:21:15 (platform/prod between legs): the live set is per stack — P12 on network, P11 on cluster — so a read of that state is not drift", "2026-09-01T14:21:15Z",
          lambda db: sorted((r["freight"], r["role"]) for r in q(db, "SELECT freight, role FROM v_live WHERE stage='platform/prod'")) == [("P11", "stable"), ("P12", "stable")]
          and grid(db, "platform/prod")["live"] == "P11 stable 100% | P12 stable 100%")

    def m_observe_between_legs(fs):
        fs.append({"id": "m030", "ts": "2026-09-01T14:21:10Z", "class": "observation", "kind": "state.observed", "subject": "stage:platform/prod",
                   "actor": "watch:conformance", "payload": {"services": {"network": "P12", "cluster": "P11"}, "source": "stack tags"}})
    check("mutation: a conformance read between legs matches the per-stack live set — partial is not drift", "2026-09-01T14:21:15Z",
          lambda db: one(db, "SELECT mismatches FROM v_observed WHERE stage='platform/prod'")["mismatches"] == 0
          and grid(db, "platform/prod")["status"] == "partial" and grid(db, "platform/prod")["drift"] is None, m_observe_between_legs)

    # --- the canary: both live inside the pause, then the abort ---------------------
    K1 = "2026-09-02T11:20:00Z"
    check("as of Wed 11:20: payments/prod is in flight, paused on step 2 of 3 with the step gate awaiting the analysis; carried is still A231", K1,
          lambda db: (lambda r: r["status"] == "in-flight" and r["inflight_freight"] == "A232" and r["carried"] == "A231" and r["desired"] == "A232"
                      and r["last_phase"] == "paused" and r["last_step"] == 1 and r["last_weight"] == 10
                      and r["step_kind"] == "pause" and r["n_steps"] == 3 and r["step_passes"] == 0
                      and r["step_awaiting"].startswith("verification: canary-analysis in payments/prod"))
          (grid(db, "payments/prod")))
    check("as of Wed 11:20: the stage carries a set — A231 stable at 90%, A232 canary at 10% — and the conformance read matches both", K1,
          lambda db: [(r["freight"], r["role"], r["weight"]) for r in q(db, "SELECT * FROM v_live WHERE stage='payments/prod' ORDER BY role DESC")]
          == [("A231", "stable", 90), ("A232", "canary", 10)]
          and grid(db, "payments/prod")["live"] == "A231 stable 90% | A232 canary 10%"
          and (lambda r: r["mismatches"] == 0 and r["expected"] == "A231 (stable 90%) | A232 (canary 10%)")(one(db, "SELECT * FROM v_observed WHERE stage='payments/prod'"))
          and one(db, "SELECT role FROM v_observed_service WHERE stage='payments/prod' AND service='payments-canary'")["role"] == "canary"
          and grid(db, "payments/prod")["drift"] is None)
    check("as of Wed 11:20: the step gate's terms are floored at the rollout's start — the stage-gate approval from 11:00 does not promote the canary", K1,
          lambda db: [(r["type"], r["satisfied_at"]) for r in q(db, "SELECT type, satisfied_at FROM v_step_term ORDER BY term_idx")]
          == [("verified", None), ("approved", None)]
          and one(db, "SELECT unmet_text FROM v_step_term WHERE type='approved'")["unmet_text"].endswith("given after 2026-09-02T11:01:00Z"))
    check("as of Wed 11:20: lanes and trace say in flight for A232 / #4440 at prod; the pin-set is still complete in prod (A231 carried)", K1,
          lambda db: one(db, "SELECT cell FROM v_lanes WHERE freight='A232' AND stage='payments/prod'")["cell"] == "in-flight"
          and one(db, "SELECT cell FROM v_trace_cell WHERE pr=4440 AND stage='payments/prod'")["cell"] == "in-flight"
          and one(db, "SELECT state FROM v_pinset_status WHERE environment='prod'")["state"] == "complete")
    K2 = "2026-09-02T11:45:00Z"
    check("as of Wed 11:45: the abort is a reversal — desired back to A231, which prod carries; A232 rolled back before it ever carried; its approval lapsed", K2,
          lambda db: (lambda r: r["desired"] == "A231" and r["carried"] == "A231" and r["carried_since"] == "2026-09-01T16:35:00Z"
                      and r["rolled_back_from"] == "A232" and r["rolled_back_at"] == "2026-09-02T11:33:05Z"
                      and r["live"] == "A231 stable 100%" and r["drift"] is None
                      and r["status"] == "awaiting" and r["candidate"] == "A232" and r["awaiting_type"] == "approved"
                      and r["awaiting_since"] == "2026-09-02T11:33:05Z")
          (grid(db, "payments/prod"))
          and one(db, "SELECT passes FROM v_gate WHERE stage='payments/prod' AND freight='A232'")["passes"] == 0
          and (lambda r: r["cell"] == "rolled-back" and r["last_outcome"] == "abandoned" and r["reached_at"] is None and r["rolled_back_to"] == "A231")
          (one(db, "SELECT * FROM v_lanes WHERE freight='A232' AND stage='payments/prod'"))
          and one(db, "SELECT cell FROM v_trace_cell WHERE pr=4440 AND stage='payments/prod'")["cell"] == "rolled-back")
    check("as of Wed 11:45: the auto-abort decision is a policy decision in the rollback direction under prod's rollback block (no approval needed), citing the failed analysis; nothing flagged", K2,
          lambda db: q(db, "SELECT * FROM v_audit_flag WHERE flag IS NOT NULL") == []
          and (lambda r: r["direction"] == "rollback" and r["from_freight"] == "A232" and r["mode"] == "auto" and r["n_required"] == 0 and r["n_refs"] == 1)
          (one(db, "SELECT * FROM v_audit_flag WHERE stage='payments/prod' AND freight='A231' AND ts='2026-09-02T11:33:05Z'"))
          and (lambda r: r["outcome"] == "abandoned" and r["last_weight"] == 10)(one(db, "SELECT * FROM v_transition WHERE transition='transition:A232-payments-prod'")))

    # --- every policy-written decision passed its gate at its own instant ------------
    # The ledger as it stood when the decision was written: everything that
    # arrived before it — not the decision itself (which would already have
    # moved intent, and with it the pair's direction), and not a fact from the
    # same second that arrived after it.
    def before(fid):
        def cut(fs):
            del fs[fs.index(find(fs, id=fid)):]
        return cut

    for f in facts:
        if f["kind"] == "promotion.decided" and f["actor"].startswith("policy:"):
            stage, freight = f["subject"][6:], f["payload"]["freight"]
            check(f"policy decision {f['id']} ({stage} ← {freight}) passed its gate when written", f["ts"],
                  (lambda s, fr: lambda db: one(db, "SELECT passes FROM v_gate WHERE stage=? AND freight=?", s, fr)["passes"] == 1)(stage, freight),
                  before(f["id"]))
        if f["kind"] == "uptake.decided" and f["actor"] == "policy:uptake":
            edge, version = f["subject"], f["payload"]["record_version"]
            check(f"policy uptake {f['id']} ({edge.split('<-')[0][5:]} ← v{version}) passed its edge gate when written", f["ts"],
                  (lambda e, v: lambda db: (lambda r: r is None or r["passes"] == 1)
                   (one(db, "SELECT passes FROM v_uptake_gate WHERE edge=? AND version=?", e, v)))(edge, version),
                  before(f["id"]))

    # --- mutations: break one mechanism, expect the views to say so --------------------
    def m_smoke_before_retry(fs):
        find(fs, kind="verification.recorded", subject="stage:platform/staging", p_freight="P12", p_check="smoke")["ts"] = "2026-09-01T10:30:00Z"
    check("mutation: a staging smoke verification recorded after the network leg but before the cluster leg does not count — the stage had not carried P12 on every stack", "2026-09-01T13:00:00Z",
          lambda db: grid(db, "platform/prod")["awaiting"].startswith("verification: smoke in platform/staging (recorded before"), m_smoke_before_retry)

    def m_no_edge_approval(fs):
        a = find(fs, kind="approval.granted", subject=E_PROD, p_record_version=4)
        d = find(fs, kind="uptake.decided", subject=E_PROD, p_record_version=4)
        d["refs"] = [r for r in d.get("refs", []) if r != a["id"]]
        fs.remove(a)
    check("mutation: a policy-written uptake with no approval on record is flagged by the uptake audit", E,
          lambda db: (lambda r: r["flag"] == "unmet at decision time: approval: payments-oncall")
          (one(db, "SELECT * FROM v_uptake_audit_flag WHERE edge=? AND version=4", E_PROD)), m_no_edge_approval)

    def m_person_uptake(fs):
        find(fs, kind="uptake.decided", subject=E_PROD, p_record_version=4)["actor"] = "user:sam"
    check("mutation: an uptake typed by a person on a gated edge is flagged", E,
          lambda db: one(db, "SELECT flag FROM v_uptake_audit_flag WHERE edge=? AND version=4", E_PROD)["flag"].startswith("decided by user:sam"), m_person_uptake)

    def m_safe_preview(fs):
        pv = find(fs, kind="plan.summarized", subject="stage:payments/staging", p_freight="A231")
        pv["payload"]["replace"] = 0
    check("mutation: a safe preview opens the staging edge without an approval (term outcome auto)", C,
          lambda db: (lambda r: r["passes"] == 1 and r["pending"] == 1)(pu(db, "payments@payments/staging"))
          and one(db, "SELECT term_outcome FROM v_uptake_term WHERE edge=? AND version=4", E_STG)["term_outcome"] == "auto", m_safe_preview)

    def m_wrong_edge_role(fs):
        e = find(fs, kind="binding.declared", subject=E_PROD)
        e["payload"]["terms"] = [{"type": "not_held"}, {"type": "approved", "role": "sre"}]
    check("mutation: an approval by the wrong role does not open the prod edge — the gate waits on sre", A,
          lambda db: (lambda r: r["pending"] == 1 and r["passes"] == 0 and r["awaiting"] == "approval: sre")(pu(db, "payments@payments/prod")),
          m_wrong_edge_role)
    check("mutation: … and the uptake the policy wrote anyway on Tuesday evening is flagged by the audit (the ledger records it; the audit distinguishes it)", E,
          lambda db: pu(db, "payments@payments/prod")["pending"] == 0
          and one(db, "SELECT flag FROM v_uptake_audit_flag WHERE edge=? AND version=4", E_PROD)["flag"] == "unmet at decision time: approval: sre",
          m_wrong_edge_role)

    def m_hold_payments_prod(fs):
        fs.append({"id": "m001", "ts": "2026-09-01T15:00:00Z", "class": "intent", "kind": "hold.placed", "subject": "stage:payments/prod",
                   "actor": "user:sam", "payload": {"until": "2026-09-01T20:00:00Z", "scope": "promotions"}, "rationale": "payments freeze"})
    check("mutation: a hold on payments/prod blocks the prod edge on its not_held term first", A,
          lambda db: pu(db, "payments@payments/prod")["awaiting"] == "hold until 2026-09-01T20:00:00Z", m_hold_payments_prod)

    def m_no_auto_uptake(fs):
        u = find(fs, kind="uptake.decided", subject=E_NET_PROD, p_record_version=2)
        for f in fs:
            if u["id"] in f.get("refs", []):
                f["refs"] = [r for r in f["refs"] if r != u["id"]]
        fs.remove(u)
    check("mutation: without the auto uptake, network v2 is pending on the prod cluster edge and the gate (no terms) passes — the policy owes a decision", A,
          lambda db: (lambda r: r["pending"] == 1 and r["passes"] == 1 and r["consumed_version"] == 1)(pu(db, "cluster@platform/prod"))
          and one(db, "SELECT count(*) AS n FROM v_pending_uptake WHERE pending = 1")["n"] == 2, m_no_auto_uptake)

    def m_cluster_leg_open(fs):
        fs.remove(find(fs, kind="transition.finished", subject="transition:P12-cluster-prod"))
        fs.remove(find(fs, kind="output.published", subject="record:cluster@platform/prod", p_version=4))
        fs.remove(find(fs, kind="plan.summarized", subject="stage:payments/prod", p_freight="A230"))
    check("mutation: with the prod cluster leg still open, platform/prod is in flight, P12 has reached nothing there, the pin-set is pending in prod, nothing is pending on the prod edge", A,
          lambda db: grid(db, "platform/prod")["status"] == "in-flight"
          and one(db, "SELECT cell FROM v_lanes WHERE freight='P12' AND stage='platform/prod'")["cell"] == "in-flight"
          and one(db, "SELECT state FROM v_pinset_status WHERE environment='prod'")["state"] == "pending"
          and pu(db, "payments@payments/prod")["pending"] == 0, m_cluster_leg_open)

    def m_unpinned(fs):
        find(fs, kind="freight.discovered", subject="freight:A231")["payload"]["config"]["base_image_version"] = 40
    check("mutation: if no freight carries the pin, the by-version edge says so — uptake decided, but every stage unpinned", A,
          lambda db: one(db, "SELECT pinned_in FROM v_pin_uptake")["pinned_in"] is None
          and all(r["pin_state"] == "unpinned" for r in q(db, "SELECT * FROM v_pin_stage")), m_unpinned)

    def m_uptake_only_enactment(fs):
        fs.append({"id": "m002", "ts": "2026-09-01T16:00:00Z", "class": "observation", "kind": "transition.started", "subject": "transition:m-uptake",
                   "actor": "ci:gha", "payload": {"stage": "payments/prod", "freight": None, "stack": "payments", "run": "gha:x", "record_version": 4}})
        fs.append({"id": "m003", "ts": "2026-09-01T16:02:00Z", "class": "observation", "kind": "transition.finished", "subject": "transition:m-uptake",
                   "actor": "ci:gha", "payload": {"outcome": "succeeded", "ops_update": 9, "summary": {"create": 0, "update": 1, "delete": 0, "replace": 0}}})
    check("mutation: an uptake-only enactment (freight NULL, the pulumi-service shape) does not disturb what payments/prod carries or since when", "2026-09-01T16:05:00Z",
          lambda db: (lambda r: r["status"] == "awaiting" and r["carried"] == "A230" and r["carried_since"] == "2026-08-25T14:20:00Z")(grid(db, "payments/prod")),
          m_uptake_only_enactment)

    def m_both_legs_open(fs):
        fs.remove(find(fs, kind="transition.finished", subject="transition:P12-network-prod"))
        fs.remove(find(fs, kind="output.published", subject="record:network@platform/prod", p_version=2))
        u = find(fs, kind="uptake.decided", subject=E_NET_PROD, p_record_version=2)
        for f in fs:
            if u["id"] in f.get("refs", []):
                f["refs"] = [r for r in f["refs"] if r != u["id"]]
        fs.remove(u)
    check("mutation: two legs open at once in platform/prod — the grid names both, not just the latest", "2026-09-01T14:30:00Z",
          lambda db: (lambda r: r["status"] == "in-flight" and r["n_inflight"] == 2 and r["inflight_detail"] == "cluster@P12, network@P12")(grid(db, "platform/prod")),
          m_both_legs_open)

    def m_verified_term_on_edge(fs):
        e = find(fs, kind="binding.declared", subject=E_PROD)
        e["payload"]["terms"] = [{"type": "verified", "stage": "platform/prod", "check": "smoke"}]
    check("mutation: a verified term on an edge is undefined, not silently passed — the gate stays closed and says why", A,
          lambda db: (lambda r: r["passes"] == 0 and r["awaiting"] == "verified is not defined on an edge")(pu(db, "payments@payments/prod")),
          m_verified_term_on_edge)

    def m_cross_program_pr(fs):
        find(fs, kind="freight.discovered", subject="freight:A231")["payload"]["warehouse"] = "platform"
    check("mutation: a payments build attributed to the platform warehouse leaks its PRs into platform membership (P12 'contains' #4428) — the warehouse key is load-bearing", A,
          lambda db: one(db, "SELECT count(*) AS n FROM v_membership WHERE warehouse='platform' AND pr=4428 AND freight='P12'")["n"] == 1
          and one(db, "SELECT reached_at FROM v_trace WHERE pr=4428 AND freight='P12' AND stage='platform/prod'")["reached_at"] == "2026-09-01T14:50:00Z",
          m_cross_program_pr)

    # --- canary mutations ---------------------------------------------------------------
    def m_promote(fs):
        fs.append({"id": "m020", "ts": "2026-09-02T11:25:00Z", "class": "observation", "kind": "verification.recorded", "subject": "stage:payments/prod",
                   "actor": "watch:argo-analysis", "payload": {"freight": "A232", "check": "canary-analysis", "outcome": "pass", "detail": "error rate 0.1%; p99 flat"}})
        fs.append({"id": "m021", "ts": "2026-09-02T11:28:00Z", "class": "intent", "kind": "approval.granted", "subject": "stage:payments/prod",
                   "actor": "user:dana", "payload": {"freight": "A232", "role": "payments-oncall", "via": "kubectl argo rollouts promote payments-api"}, "rationale": "analysis clean; promote"})
    check("mutation: a passing analysis and a promote given after the rollout started open the step gate — the executor may continue", "2026-09-02T11:30:00Z",
          lambda db: (lambda r: r["status"] == "in-flight" and r["step_passes"] == 1)(grid(db, "payments/prod"))
          and one(db, "SELECT evidence_fact FROM v_step_term WHERE type='approved'")["evidence_fact"] == "m021", m_promote)

    def m_stale_analysis(fs):
        fs.append({"id": "m022", "ts": "2026-09-02T10:50:00Z", "class": "observation", "kind": "verification.recorded", "subject": "stage:payments/prod",
                   "actor": "watch:argo-analysis", "payload": {"freight": "A232", "check": "canary-analysis", "outcome": "pass", "detail": "a run from before the rollout"}})
    check("mutation: an analysis recorded before the rollout started does not satisfy the step gate", K1,
          lambda db: (lambda r: r["step_passes"] == 0 and r["step_awaiting"].startswith("verification: canary-analysis"))(grid(db, "payments/prod")), m_stale_analysis)

    def m_wrong_canary(fs):
        find(fs, kind="state.observed", subject="stage:payments/prod", ts="2026-09-02T11:15:00Z")["payload"]["services"]["payments-canary"] = "A230"
    check("mutation: a canary running something neither carried nor in flight is drift, unexplained", K1,
          lambda db: (lambda r: r["mismatches"] == 1 and r["mismatch_detail"] == "payments-canary@A230")(one(db, "SELECT * FROM v_observed WHERE stage='payments/prod'"))
          and grid(db, "payments/prod")["drift"].endswith("UNEXPLAINED"), m_wrong_canary)

    def m_no_weights(fs):
        for f in fs:
            if f["kind"] == "transition.phase" and f["subject"] == "transition:A232-payments-prod":
                f["payload"].pop("weight", None)
    check("mutation: without reported weights the live set is still both freights — conformance is on the set, weights are decoration", K1,
          lambda db: grid(db, "payments/prod")["live"] == "A231 stable | A232 canary"
          and one(db, "SELECT mismatches FROM v_observed WHERE stage='payments/prod'")["mismatches"] == 0, m_no_weights)

    def m_no_rollback_block(fs):
        find(fs, kind="policy.declared", subject="stage:payments/prod")["payload"].pop("rollback")
    check("mutation: without prod's rollback block the auto-abort is judged by the forward gate, whose approval for A231 lapsed — the audit flags it", K2,
          lambda db: (lambda r: r["flag"] == "unmet at decision time: approval: payments-oncall")
          (one(db, "SELECT * FROM v_audit_flag WHERE stage='payments/prod' AND freight='A231' AND ts='2026-09-02T11:33:05Z'")), m_no_rollback_block)

    def m_abort_uncited(fs):
        d = find(fs, kind="promotion.decided", subject="stage:payments/prod", ts="2026-09-02T11:33:05Z")
        d["refs"] = []
    check("mutation: an abort that cites no evidence is flagged as unevidenced, though its gate (carried before) passes", K2,
          lambda db: one(db, "SELECT flag FROM v_audit_flag WHERE stage='payments/prod' AND freight='A231' AND ts='2026-09-02T11:33:05Z'")["flag"] == "no evidence cited",
          m_abort_uncited)

    def m_reapprove_a232(fs):
        fs.append({"id": "m023", "ts": "2026-09-02T11:40:00Z", "class": "intent", "kind": "approval.granted", "subject": "stage:payments/prod",
                   "actor": "user:dana", "payload": {"freight": "A232", "role": "payments-oncall", "via": "pulumi delivery approve payments/prod"}, "rationale": "retry storm fixed by config; try again"})
    check("mutation: a fresh approval after the abort makes A232 ready again at prod", K2,
          lambda db: grid(db, "payments/prod")["status"] == "ready", m_reapprove_a232)
