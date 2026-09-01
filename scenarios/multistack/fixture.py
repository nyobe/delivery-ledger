#!/usr/bin/env python3
"""Two programs, no Uber program — the fixture, written as a timeline.

    ./fixture.py            # rewrite facts.jsonl

A platform team (network + cluster stacks) and a payments team (one stack)
each run their own delivery program over dev → staging → prod. Nothing owns
the whole graph: the edges between them are bindings declared on the
consumer, one pattern per program instantiated per environment, and every
cross-team uptake is the consuming team's own decision under its own policy.

The story is a Kubernetes 1.31 upgrade that has to ship as a pin-set across
both teams: the platform freight P12 upgrades the cluster (rotating its OIDC
issuer) and adds a subnet; the payments freight A231 carries the base image
the new cluster needs, pinned by a bot PR that auto-merged on green checks.
Both binding kinds are exercised side by side — see program.md.

Everything here is illustrative: the teams, stacks, PR numbers and timings
are invented to exercise the mechanism, not taken from a real pipeline (the
pulumi-service scenario is the grounded one). The shapes are grounded in the
notebook: Tyler's VPC → cluster → app split with no shared program, the
second customer's "different lifecycles, different governance" criterion for
subject boundaries, Moderna's hyper-preview, and the by-version AMI path.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FACTS = []


def T(month, day, hms):
    return f"2026-{month:02d}-{day:02d}T{hms}Z"


def fact(ts, cls, kind, subject, actor, payload, rationale=None, refs=()):
    f = {"ts": ts, "class": cls, "kind": kind, "subject": subject, "actor": actor,
         "payload": payload, "rationale": rationale, "refs": list(refs)}
    FACTS.append(f)
    return f


def intent(*a, **k):
    return fact(*a[:1], "intent", *a[1:], **k)


def observe(*a, **k):
    return fact(*a[:1], "observation", *a[1:], **k)


# ---------------------------------------------------------------------------
# Gate terms — the same five types as pulumi-service; policies and edges both
# carry them. `rule` text is rendered from the terms.
# ---------------------------------------------------------------------------

def verified(stage, check):
    return {"type": "verified", "stage": stage, "check": check}


def carried(stage):
    return {"type": "carried", "stage": stage}


def approved(role, via=None):
    t = {"type": "approved", "role": role}
    if via:
        t["via"] = via
    return t


def not_held():
    return {"type": "not_held"}


def plan_safe_or_approved(role):
    return {"type": "plan_safe_or_approved", "role": role}


def rule_text(subject, terms):
    parts = []
    for t in terms:
        k = t["type"]
        if k == "verified":
            parts.append(f"verified({t['stage']}, F, '{t['check']}')")
        elif k == "carried":
            parts.append(f"carried({t['stage']}, F)")
        elif k == "approved":
            parts.append(f"approved({subject}, F, role = '{t['role']}')")
        elif k == "not_held":
            parts.append(f"NOT held({subject})")
        elif k == "plan_safe_or_approved":
            parts.append(f"(plan({subject}, F).safe OR approved({subject}, F, role = '{t['role']}'))")
    return " AND ".join(parts) or "true"


SAFE_RULE = "plan.delete = 0 AND plan.replace = 0 AND NOT plan.migrations_changed"

ENVS = ["dev", "staging", "prod"]
REGION = {"dev": "us-west-2", "staging": "us-west-2", "prod": "us-west-2"}

# ---------------------------------------------------------------------------
# Declarations — two delivery programs, applied 2026-08-24 by their own teams
# ---------------------------------------------------------------------------

VIA_P = "delivery program acme/platform rev 4"
VIA_A = "delivery program acme/payments rev 2"
t0 = T(8, 24, "15:00:00")

intent(t0, "warehouse.declared", "warehouse:platform", "user:rafael",
       {"program": "platform", "repo": "acme/platform", "branch": "main", "stacks": ["network", "cluster"],
        "images": [], "config": "esc:acme/platform", "via": VIA_P})
intent(T(8, 24, "15:00:01"), "warehouse.declared", "warehouse:payments", "user:dana",
       {"program": "payments", "repo": "acme/payments", "branch": "main", "stacks": ["payments"],
        "images": ["payments-api"], "config": "esc:acme/payments (base_image_version pinned here)", "via": VIA_A})


def stage(program, env, ord_, owner, slack, upstream, stacks, actor, via, **extra):
    name = f"{program}/{env}"
    p = dict({"order": ord_, "display": name, "program": program, "environment": env, "region": REGION[env],
              "owner": owner, "slack": slack, "upstream": upstream, "stacks": stacks, "via": via}, **extra)
    return intent(T(8, 24, f"15:00:{ord_ + 1:02d}"), "stage.declared", f"stage:{name}", actor, p)


stage("platform", "dev",     1, "platform-eng", "#platform-dev",   "warehouse:platform",  ["network", "cluster"], "user:rafael", VIA_P)
stage("platform", "staging", 2, "platform-eng", "#platform-ops",   "platform/dev",        ["network", "cluster"], "user:rafael", VIA_P)
stage("platform", "prod",    3, "platform-eng", "#platform-ops",   "platform/staging",    ["network", "cluster"], "user:rafael", VIA_P,
      ops="acme/platform/prod")
stage("payments", "dev",     4, "payments",     "#payments-dev",   "warehouse:payments",  ["payments"], "user:dana", VIA_A)
stage("payments", "staging", 5, "payments",     "#payments-ops",   "payments/dev",        ["payments"], "user:dana", VIA_A)
stage("payments", "prod",    6, "payments",     "#payments-ops",   "payments/staging",    ["payments"], "user:dana", VIA_A,
      url="https://pay.acme.example", ops="acme/payments/prod")


def policy(name, mode, trigger, terms, desc, actor, i):
    p = {"mode": mode, "terms": terms, "rule": rule_text(name, terms), "description": desc}
    if trigger:
        p["trigger"] = trigger
    if any(t["type"] == "plan_safe_or_approved" for t in terms):
        p["safe"] = SAFE_RULE
    return intent(T(8, 24, f"15:00:{10 + i:02d}"), "policy.declared", f"stage:{name}", actor, p)


policy("platform/dev",     "auto",  "freight.discovered on warehouse:platform", [],
       "Every main build of the platform rolls to dev.", "user:rafael", 0)
policy("platform/staging", "auto",  None, [verified("platform/dev", "smoke")],
       "Staging follows dev once the smoke suite passes there.", "user:rafael", 1)
policy("platform/prod",    "gated", None,
       [verified("platform/staging", "smoke"), verified("platform/staging", "soak"), not_held(),
        approved("platform-oncall", via="pulumi delivery approve platform/prod")],
       "A platform release to prod is a decision by platform oncall after a soak in staging.", "user:rafael", 2)
policy("payments/dev",     "auto",  "freight.discovered on warehouse:payments", [],
       "Every main build of payments rolls to dev.", "user:dana", 3)
policy("payments/staging", "auto",  None, [verified("payments/dev", "integration")],
       "Staging follows dev once integration tests pass there.", "user:dana", 4)
policy("payments/prod",    "gated", None,
       [verified("payments/staging", "integration"), verified("payments/staging", "canary"),
        approved("payments-oncall", via="pulumi delivery approve payments/prod")],
       "Payments oncall decides what reaches prod.", "user:dana", 5)


# --- bindings: one pattern per program, instantiated per environment -----------

def edge_subject(consumer, producer, key):
    return f"edge:{consumer}<-{producer}.{key}"


def pattern(ts, consumer_stack, consumer_program, producer_stack, producer_program, key, kind, actor, via,
            uptake, description, environments=None, terms=None):
    """Declare a binding pattern and its instances. Returns {env: instance subject}."""
    subj = edge_subject(consumer_stack, producer_stack, key)
    p = {"role": "pattern", "consumer": consumer_stack, "producer": producer_stack, "key": key, "kind": kind,
         "consumer_program": consumer_program, "producer_program": producer_program,
         "uptake": uptake, "description": description, "via": via}
    if environments:
        p["environments"] = environments
    pat = intent(ts, "binding.declared", subj, actor, p)
    out = {}
    if kind == "by-version":
        # One instance: the pin is stage-invariant and rides the consumer's freight.
        inst = intent(ts, "binding.declared", subj + "@*", actor,
                      {"consumer": consumer_stack, "producer": producer_stack, "key": key, "kind": kind,
                       "consumer_program": consumer_program, "producer_program": producer_program,
                       "uptake": uptake, "pattern": subj, "description": description, "via": via}, refs=[pat])
        out["*"] = inst["subject"]
        return out
    for i, env in enumerate(environments):
        c = f"{consumer_stack}@{consumer_program}/{env}"
        pr = f"{producer_stack}@{producer_program}/{env}"
        u = uptake[env] if isinstance(uptake, dict) else uptake
        tm = (terms or {}).get(env, []) if isinstance(terms, dict) else (terms or [])
        q = {"consumer": c, "producer": pr, "key": key, "kind": kind, "environment": env,
             "consumer_program": consumer_program, "producer_program": producer_program,
             "uptake": u, "terms": tm, "rule": rule_text(edge_subject(c, pr, key), tm),
             "pattern": subj, "description": description, "via": via}
        if any(t["type"] == "plan_safe_or_approved" for t in tm):
            q["safe"] = SAFE_RULE
        inst = intent(ts, "binding.declared", edge_subject(c, pr, key), actor, q, refs=[pat])
        out[env] = inst["subject"]
    return out


# Within the platform team: the cluster reads the network's subnets. Same
# freight, same team — the uptake is automatic and the cluster leg of every
# platform promotion is where it lands.
E_SUBNETS = pattern(T(8, 24, "15:00:20"), "cluster", "platform", "network", "platform", "private_subnet_ids",
                    "by-reference", "user:rafael", VIA_P, uptake="auto",
                    description="The cluster's node groups live in the network's private subnets. Same team, same "
                                "freight: a new subnet list is taken up by the cluster leg automatically.",
                    environments=ENVS)

# Across teams, by reference: payments reads the cluster's endpoint and OIDC
# issuer. The uptake is the payments team's decision, under a policy that is
# automatic in dev, auto-if-safe in staging, and gated in prod.
E_CLUSTER = pattern(T(8, 24, "15:00:21"), "payments", "payments", "cluster", "platform", "cluster_endpoint",
                    "by-reference", "user:dana", VIA_A,
                    uptake={"dev": "auto", "staging": "auto-if-safe", "prod": "gated"},
                    terms={"dev": [], "staging": [plan_safe_or_approved("payments-oncall")],
                           "prod": [not_held(), approved("payments-oncall", via="pulumi delivery approve <edge>")]},
                    description="A new cluster record (endpoint, OIDC issuer) re-points the payments provider. Dev "
                                "ripples; staging ripples when the preview is boring; prod waits for payments oncall.",
                    environments=ENVS)

# Across teams, by version: payments pins the platform base image in its
# config. Uptake is a PR that bumps the pin; it rides the payments train.
E_IMAGE = pattern(T(8, 24, "15:00:22"), "payments", "payments", "platform-images", "platform", "base_image_version",
                  "by-version", "user:dana", VIA_A, uptake="auto-with-checks",
                  description="The base image version is pinned in payments' config. A bot opens the bump PR; it "
                              "auto-merges on green checks, and the new pin rides the payments train through every "
                              "stage's ordinary gates.")


# ---------------------------------------------------------------------------
# Helpers for the pipeline facts
# ---------------------------------------------------------------------------

def pr(number, title, author):
    return {"number": number, "title": title, "author": author}


def discovered(ts, warehouse, freight, sha, prs, build, config=None, images=None):
    p = {"warehouse": warehouse, "source": {"repo": f"acme/{warehouse}", "sha": sha, "branch": "main"},
         "prs": prs, "build": build, "config": config or {}}
    if images:
        p["images"] = images
    return observe(ts, "freight.discovered", f"freight:{freight}", "ci:gha", p)


def decide(ts, stg, freight, actor, rationale, refs=()):
    return intent(ts, "promotion.decided", f"stage:{stg}", actor, {"freight": freight}, rationale, refs)


def started(ts, tid, stg, freight, stack, run, refs=(), **extra):
    p = dict({"stage": stg, "freight": freight, "stack": stack, "run": run}, **extra)
    return observe(ts, "transition.started", f"transition:{tid}", "ci:gha", p, refs=refs)


def finished(ts, tid, outcome, ops_update=None, summary=None, refs=(), **extra):
    p = {"outcome": outcome}
    if ops_update is not None:
        p["ops_update"] = ops_update
    if summary is not None:
        p["summary"] = dict(zip(("create", "update", "delete", "replace"), summary))
    p.update(extra)
    return observe(ts, "transition.finished", f"transition:{tid}", "ci:gha", p, refs=refs)


def phase(ts, tid, name, detail):
    return observe(ts, "transition.phase", f"transition:{tid}", "ci:gha", {"phase": name, "detail": detail})


def verify(ts, stg, freight, check, outcome, detail, actor="ci:gha"):
    return observe(ts, "verification.recorded", f"stage:{stg}", actor,
                   {"freight": freight, "check": check, "outcome": outcome, "detail": detail})


def plan(ts, stg, freight, against, counts, note=None):
    p = {"freight": freight, "against": against}
    p.update(dict(zip(("create", "update", "delete", "replace"), counts)))
    p["migrations_changed"] = False
    if note:
        p["note"] = note
    return observe(ts, "plan.summarized", f"stage:{stg}", "ci:gha", p)


def preview(ts, stg, freight, producer, version, counts, note=None):
    """A hyper-preview: the consumer's plan against a proposed record, ahead of any uptake."""
    p = {"freight": freight, "against": f"record {producer} v{version}",
         "against_record": {"producer": producer, "version": version}}
    p.update(dict(zip(("create", "update", "delete", "replace"), counts)))
    p["migrations_changed"] = False
    if note:
        p["note"] = note
    return observe(ts, "plan.summarized", f"stage:{stg}", "ci:gha", p)


def approve(ts, stg, freight, actor, role, via, rationale, refs=()):
    return intent(ts, "approval.granted", f"stage:{stg}", actor, {"freight": freight, "role": role, "via": via}, rationale, refs)


def approve_uptake(ts, edge, version, actor, role, via, rationale, refs=()):
    return intent(ts, "approval.granted", edge, actor, {"record_version": version, "role": role, "via": via}, rationale, refs)


def publish(ts, producer, version, values, produced_by, **extra):
    return observe(ts, "output.published", f"record:{producer}", "ci:gha",
                   dict({"version": version, "values": values, "produced_by": produced_by}, **extra))


def uptake(ts, edge, version, actor, rationale, refs=(), **extra):
    return intent(ts, "uptake.decided", edge, actor, dict({"record_version": version}, **extra), rationale, refs)


def hold(ts, stg, until, actor, rationale):
    return intent(ts, "hold.placed", f"stage:{stg}", actor, {"until": until, "scope": "promotions"}, rationale)


def observed(ts, stg, stacks):
    return observe(ts, "state.observed", f"stage:{stg}", "watch:conformance",
                   {"services": stacks, "source": "stack tags on the deployed resources"})


def add_min(hms, minutes):
    h, m, s = (int(x) for x in hms.split(":"))
    m += minutes
    h += m // 60
    m %= 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def platform_promotion(month, day, hms, env, freight, decision, net_summary, cl_summary, net_values=None,
                       cl_values=None, net_version=None, cl_version=None, net_minutes=10, cl_minutes=25,
                       cluster_phases=None, gap=1):
    """Enact a platform promotion: network leg, publish, auto-uptake, cluster leg, publish.

    Returns (network_finished, cluster_finished, cluster_record_fact_or_None)."""
    stg = f"platform/{env}"
    run = f"gha:platform-deploy/{freight[1:]}{env[0]}"
    t_net = hms
    n = started(T(month, day, t_net), f"{freight}-network-{env}", stg, freight, "network", run, [decision])
    nf = finished(T(month, day, add_min(t_net, net_minutes)), f"{freight}-network-{env}", "succeeded",
                  1000 + int(freight[1:]), net_summary)
    refs = [decision]
    rec = None
    if net_values is not None:
        # the publication lands 30s after the leg; the auto uptake 5s after that
        t_pub = add_min(t_net, net_minutes)[:-2]
        rec = publish(T(month, day, t_pub + "30"), f"network@{stg}", net_version, net_values,
                      f"{freight}-network-{env} (stack outputs)")
        u = uptake(T(month, day, t_pub + "35"), E_SUBNETS[env], net_version, "policy:uptake",
                   "auto: binding declares auto uptake within the platform program", [rec])
        refs.append(u)
    t_cl = add_min(t_net, net_minutes + gap)
    c = started(T(month, day, t_cl), f"{freight}-cluster-{env}", stg, freight, "cluster", run, refs,
                **({"record_version": net_version} if net_version else {}))
    for off, name, detail in (cluster_phases or []):
        phase(T(month, day, add_min(t_cl, off)), f"{freight}-cluster-{env}", name, detail)
    cf = finished(T(month, day, add_min(t_cl, cl_minutes)), f"{freight}-cluster-{env}", "succeeded",
                  2000 + int(freight[1:]), cl_summary)
    crec = None
    if cl_values is not None:
        crec = publish(T(month, day, add_min(t_cl, cl_minutes)[:-2] + "30"), f"cluster@{stg}", cl_version, cl_values,
                       f"{freight}-cluster-{env} (stack outputs)")
    return nf, cf, crec


def payments_enact(month, day, hms, env, freight, refs, record_version, summary, minutes=10, tid=None):
    stg = f"payments/{env}"
    tid = tid or f"{freight}-payments-{env}"
    started(T(month, day, hms), tid, stg, freight, "payments", f"gha:payments-deploy/{freight[1:]}{env[0]}", refs,
            record_version=record_version)
    return finished(T(month, day, add_min(hms, minutes)), tid, "succeeded", 3000 + int(freight[1:]), summary)


# ---------------------------------------------------------------------------
# Baseline — the week before: P11 and A230 everywhere; records v1/v3; image v40
# ---------------------------------------------------------------------------

img40 = publish(T(8, 24, "16:00:00"), "platform-images", 40, {"base_image_version": 40, "image": "acme/base:1.30-4"},
                "platform-images bake #140", base="ubuntu-24.04")
u40 = uptake(T(8, 24, "16:30:00"), E_IMAGE["*"], 40, "bot:renovate",
             "scheduled dependency update; checks green", [img40], via="PR #4402 auto-merged on green checks", pr=4402)

# platform P11 — Tue 08-25
d11 = discovered(T(8, 25, "09:00:00"), "platform", "P11", "6f1a2c0",
                 [pr(801, "cluster: 1.30 managed node pool", "rafael"), pr(799, "network: VPC flow logs", "mei")],
                 "gha:platform-build/611")
dec = decide(T(8, 25, "09:01:00"), "platform/dev", "P11", "policy:platform/dev", "auto: platform build discovered", [d11])
_, cf, crec = platform_promotion(8, 25, "09:02:00", "dev", "P11", dec, (0, 4, 0, 0), (0, 6, 0, 0),
                                 net_values={"private_subnet_ids": ["subnet-0a1", "subnet-0a2"]}, net_version=1,
                                 cl_values={"cluster_endpoint": "https://d3v.eks.example", "oidc_issuer": "oidc.eks/D3V1"}, cl_version=3)
u_dev3 = uptake(T(8, 25, "09:39:35"), E_CLUSTER["dev"], 3, "policy:uptake", "auto: binding declares auto uptake in dev", [crec])
verify(T(8, 25, "09:45:00"), "platform/dev", "P11", "smoke", "pass", "cluster reachable; 3/3 addons healthy")
dec = decide(T(8, 25, "09:46:00"), "platform/staging", "P11", "policy:platform/staging", "auto: dev carries and verified P11", [cf])
_, cf, crec = platform_promotion(8, 25, "09:47:00", "staging", "P11", dec, (0, 4, 0, 0), (0, 6, 0, 0),
                                 net_values={"private_subnet_ids": ["subnet-1b1", "subnet-1b2"]}, net_version=1,
                                 cl_values={"cluster_endpoint": "https://stg.eks.example", "oidc_issuer": "oidc.eks/STG1"}, cl_version=3)
pv = preview(T(8, 25, "10:25:00"), "payments/staging", None, "cluster@platform/staging", 3, (0, 0, 0, 0),
             note="first record on this edge; nothing deployed yet reads it")
u_stg3 = uptake(T(8, 25, "10:26:00"), E_CLUSTER["staging"], 3, "policy:uptake", "auto: preview is safe (0 deletes, 0 replaces)", [crec, pv])
verify(T(8, 25, "11:00:00"), "platform/staging", "P11", "smoke", "pass", "cluster reachable; 3/3 addons healthy")
verify(T(8, 25, "12:00:00"), "platform/staging", "P11", "soak", "pass", "60m soak: no node restarts, API p99 < 200ms", actor="watch:soak")
a11 = approve(T(8, 25, "13:00:00"), "platform/prod", "P11", "user:rafael", "platform-oncall", "pulumi delivery approve platform/prod",
              "staging soaked clean; routine platform release")
dec = decide(T(8, 25, "13:00:05"), "platform/prod", "P11", "policy:platform/prod",
             "gate satisfied: verified in staging (smoke, soak), no hold, approved by platform-oncall", [a11])
_, cf11p, crec = platform_promotion(8, 25, "13:01:00", "prod", "P11", dec, (0, 4, 0, 0), (0, 6, 0, 0),
                                    net_values={"private_subnet_ids": ["subnet-9c1", "subnet-9c2"]}, net_version=1,
                                    cl_values={"cluster_endpoint": "https://prd.eks.example", "oidc_issuer": "oidc.eks/PRD1"}, cl_version=3)
ap3 = approve_uptake(T(8, 25, "13:50:00"), E_CLUSTER["prod"], 3, "user:dana", "payments-oncall", "pulumi delivery approve edge",
                     "first prod cluster record; payments not deployed yet, nothing to break", [crec])
u_prd3 = uptake(T(8, 25, "13:50:05"), E_CLUSTER["prod"], 3, "policy:uptake", "gate satisfied: no hold; approved by payments-oncall", [crec, ap3])

# payments A230 — Tue 08-25, after the platform is up
d230 = discovered(T(8, 25, "10:00:00"), "payments", "A230", "b7e4d19",
                  [pr(4410, "checkout: idempotency keys", "sam"), pr(4402, "chore: bump base image to v40", "renovate")],
                  "gha:payments-build/2230", config={"base_image_version": 40}, images={"payments-api": "sha256:a230aa"})
dec = decide(T(8, 25, "10:01:00"), "payments/dev", "A230", "policy:payments/dev", "auto: payments build discovered", [d230])
f = payments_enact(8, 25, "10:02:00", "dev", "A230", [dec, u_dev3], 3, (12, 0, 0, 0))
verify(T(8, 25, "10:25:00"), "payments/dev", "A230", "integration", "pass", "218 tests, 0 failures")
dec = decide(T(8, 25, "10:27:00"), "payments/staging", "A230", "policy:payments/staging", "auto: dev carries and verified A230", [f])
f = payments_enact(8, 25, "10:28:00", "staging", "A230", [dec, u_stg3], 3, (12, 0, 0, 0))
verify(T(8, 25, "10:55:00"), "payments/staging", "A230", "integration", "pass", "218 tests, 0 failures")
verify(T(8, 25, "11:25:00"), "payments/staging", "A230", "canary", "pass", "30m canary at 10%: error rate 0.02%", actor="watch:canary")
a230 = approve(T(8, 25, "14:00:00"), "payments/prod", "A230", "user:dana", "payments-oncall", "pulumi delivery approve payments/prod",
               "staging canary clean")
dec = decide(T(8, 25, "14:00:05"), "payments/prod", "A230", "policy:payments/prod",
             "gate satisfied: verified in staging (integration, canary), approved by payments-oncall", [a230])
payments_enact(8, 25, "14:01:00", "prod", "A230", [dec, u_prd3], 3, (12, 0, 0, 0), minutes=19)
verify(T(8, 25, "14:35:00"), "payments/prod", "A230", "integration", "pass", "218 tests, 0 failures")

# ---------------------------------------------------------------------------
# The upgrade week — Tue 09-01
# ---------------------------------------------------------------------------

# 08:00 the platform bake publishes a 1.31-compatible base image; the bot pins it into payments.
img41 = publish(T(9, 1, "08:00:00"), "platform-images", 41, {"base_image_version": 41, "image": "acme/base:1.31-1"},
                "platform-images bake #147", base="ubuntu-24.04", note="cgroup v2 only; required by Kubernetes 1.31 node pools")
u41 = uptake(T(9, 1, "08:35:00"), E_IMAGE["*"], 41, "bot:renovate",
             "scheduled dependency update; checks green", [img41], via="PR #4431 auto-merged on green checks", pr=4431)
d231 = discovered(T(9, 1, "08:40:00"), "payments", "A231", "c91f0e2",
                  [pr(4431, "chore: bump base image to v41", "renovate"), pr(4428, "ledger export endpoint", "sam")],
                  "gha:payments-build/2231", config={"base_image_version": 41}, images={"payments-api": "sha256:a231bb"})
dec = decide(T(9, 1, "08:41:00"), "payments/dev", "A231", "policy:payments/dev", "auto: payments build discovered", [d231])
f = payments_enact(9, 1, "08:42:00", "dev", "A231", [dec], 3, (0, 1, 0, 0))
verify(T(9, 1, "09:05:00"), "payments/dev", "A231", "integration", "pass", "221 tests, 0 failures")
dec = decide(T(9, 1, "09:06:00"), "payments/staging", "A231", "policy:payments/staging", "auto: dev carries and verified A231", [f])
f231s = payments_enact(9, 1, "09:07:00", "staging", "A231", [dec], 3, (0, 1, 0, 0))
verify(T(9, 1, "09:35:00"), "payments/staging", "A231", "integration", "pass", "221 tests, 0 failures")
verify(T(9, 1, "10:05:00"), "payments/staging", "A231", "canary", "pass", "30m canary at 10%: error rate 0.01%", actor="watch:canary")
plan(T(9, 1, "10:06:00"), "payments/prod", "A231", "A230", (0, 1, 0, 0), note="image bump only")

# 09:00 the platform's 1.31 upgrade lands on main.
d12 = discovered(T(9, 1, "09:00:00"), "platform", "P12", "e2c7b44",
                 [pr(812, "cluster: upgrade control plane and node pools to 1.31", "rafael"),
                  pr(810, "network: third private subnet for the new node pool", "mei")],
                 "gha:platform-build/624")

# The pin-set: platform first, payments right behind.
intent(T(9, 1, "09:30:00"), "release.pinned", "release:k8s-1.31", "user:rafael",
       {"display": "k8s 1.31", "members": {"platform": "P12", "payments": "A231"}, "order": ["platform", "payments"]},
       "1.31 rotates the cluster's OIDC issuer and needs the v41 base image on the nodes; ship the platform leg "
       "first and payments A231 (issuer-aware auth, v41 image) right behind it, per environment", [d12, d231])

dec = decide(T(9, 1, "09:01:00"), "platform/dev", "P12", "policy:platform/dev", "auto: platform build discovered", [d12])
_, cf, crec = platform_promotion(9, 1, "09:02:00", "dev", "P12", dec, (1, 2, 0, 0), (2, 5, 0, 1),
                                 net_values={"private_subnet_ids": ["subnet-0a1", "subnet-0a2", "subnet-0a3"]}, net_version=2,
                                 cl_values={"cluster_endpoint": "https://d3v.eks.example", "oidc_issuer": "oidc.eks/D3V2"}, cl_version=4)
u = uptake(T(9, 1, "09:39:35"), E_CLUSTER["dev"], 4, "policy:uptake", "auto: binding declares auto uptake in dev", [crec])
payments_enact(9, 1, "09:41:00", "dev", "A231", [u], 4, (0, 2, 0, 1), minutes=7, tid="A231-payments-dev-rec4")
verify(T(9, 1, "09:55:00"), "platform/dev", "P12", "smoke", "pass", "cluster reachable; 3/3 addons healthy on 1.31")

# staging: the cluster leg fails once (node pool quota), the pump retries the standing intent.
dec = decide(T(9, 1, "09:56:00"), "platform/staging", "P12", "policy:platform/staging", "auto: dev carries and verified P12", [cf])
n = started(T(9, 1, "09:57:00"), "P12-network-staging", "platform/staging", "P12", "network", "gha:platform-deploy/12s", [dec])
nf = finished(T(9, 1, "10:07:00"), "P12-network-staging", "succeeded", 1012, (1, 2, 0, 0))
rec = publish(T(9, 1, "10:07:30"), "network@platform/staging", 2, {"private_subnet_ids": ["subnet-1b1", "subnet-1b2", "subnet-1b3"]},
              "P12-network-staging (stack outputs)")
u = uptake(T(9, 1, "10:07:35"), E_SUBNETS["staging"], 2, "policy:uptake", "auto: binding declares auto uptake within the platform program", [rec])
started(T(9, 1, "10:08:00"), "P12-cluster-staging", "platform/staging", "P12", "cluster", "gha:platform-deploy/12s", [dec, u], record_version=2)
finished(T(9, 1, "10:25:00"), "P12-cluster-staging", "failed", 2012, (0, 0, 0, 0),
         error="aws:eks/nodeGroup:NodeGroup pool-131: creating: ResourceLimitExceeded: vCPU quota for m6i in us-west-2",
         failed_step="Update `platform/staging` (cluster)", step_url="gha:platform-deploy/12s#step:7:1")
started(T(9, 1, "10:45:00"), "P12-cluster-staging-2", "platform/staging", "P12", "cluster", "gha:platform-deploy/12s-2", [dec, u],
        record_version=2, retry_of="P12-cluster-staging", note="quota raised at 10:40; the standing decision is re-enacted")
finished(T(9, 1, "11:05:00"), "P12-cluster-staging-2", "succeeded", 2013, (2, 5, 0, 1))
crec = publish(T(9, 1, "11:05:30"), "cluster@platform/staging", 4,
               {"cluster_endpoint": "https://stg.eks.example", "oidc_issuer": "oidc.eks/STG2"}, "P12-cluster-staging-2 (stack outputs)")
pv = preview(T(9, 1, "11:06:00"), "payments/staging", "A231", "cluster@platform/staging", 4, (0, 2, 0, 1),
             note="the OIDC issuer changed: the kubernetes provider is replaced, and the workload identity binding with it")
ap4s = approve_uptake(T(9, 1, "11:30:00"), E_CLUSTER["staging"], 4, "user:dana", "payments-oncall", "pulumi delivery approve edge",
                      "provider replace is expected for the issuer rotation; A231 carries the issuer-aware auth lib", [pv])
u = uptake(T(9, 1, "11:30:05"), E_CLUSTER["staging"], 4, "policy:uptake",
           "gate satisfied: preview not safe (1 replace); approved by payments-oncall", [crec, pv, ap4s])
payments_enact(9, 1, "11:31:00", "staging", "A231", [u], 4, (0, 2, 0, 1), minutes=9, tid="A231-payments-staging-rec4")
verify(T(9, 1, "11:20:00"), "platform/staging", "P12", "smoke", "pass", "cluster reachable; 3/3 addons healthy on 1.31")
verify(T(9, 1, "12:20:00"), "platform/staging", "P12", "soak", "pass", "60m soak on 1.31: no node restarts, API p99 < 210ms", actor="watch:soak")

# prod: platform oncall approves; network leg, then the cluster leg with phases.
a12 = approve(T(9, 1, "14:00:00"), "platform/prod", "P12", "user:rafael", "platform-oncall", "pulumi delivery approve platform/prod",
              "staging soaked on 1.31; quota raised in prod ahead of time; payments oncall standing by for the edge")
dec12p = decide(T(9, 1, "14:00:05"), "platform/prod", "P12", "policy:platform/prod",
                "gate satisfied: verified in staging (smoke, soak), no hold, approved by platform-oncall", [a12])
_, cf12p, crec4p = platform_promotion(9, 1, "14:01:00", "prod", "P12", dec12p, (1, 2, 0, 0), (2, 5, 0, 1),
                                      net_values={"private_subnet_ids": ["subnet-9c1", "subnet-9c2", "subnet-9c3"]}, net_version=2,
                                      cl_values={"cluster_endpoint": "https://prd.eks.example", "oidc_issuer": "oidc.eks/PRD2"}, cl_version=4,
                                      net_minutes=19, cl_minutes=28, gap=2,
                                      cluster_phases=[(1, "control-plane", "control plane upgrading to 1.31"),
                                                      (12, "node-pool", "pool-131 created in three subnets; pool-130 cordoned and draining"),
                                                      (24, "retiring", "pool-130 deleted")])
preview(T(9, 1, "14:52:00"), "payments/prod", "A230", "cluster@platform/prod", 4, (0, 2, 0, 1),
        note="the OIDC issuer changed: the kubernetes provider is replaced, and the workload identity binding with it")
observed(T(9, 1, "15:00:00"), "platform/prod", {"network": "P12", "cluster": "P12"})
observed(T(9, 1, "15:00:10"), "payments/prod", {"payments": "A230"})

# --- resolution, later that afternoon: payments oncall takes both decisions ------
ap4p = approve_uptake(T(9, 1, "16:10:00"), E_CLUSTER["prod"], 4, "user:dana", "payments-oncall", "pulumi delivery approve edge",
                      "staging took the same replace cleanly at 11:31; A231 is in staging with the issuer-aware auth lib", [crec4p])
u4p = uptake(T(9, 1, "16:10:05"), E_CLUSTER["prod"], 4, "policy:uptake",
             "gate satisfied: no hold; approved by payments-oncall", [crec4p, ap4p])
a231 = approve(T(9, 1, "16:12:00"), "payments/prod", "A231", "user:dana", "payments-oncall", "pulumi delivery approve payments/prod",
               "canary clean; ships with the cluster record uptake as one enactment")
dec231 = decide(T(9, 1, "16:12:05"), "payments/prod", "A231", "policy:payments/prod",
                "gate satisfied: verified in staging (integration, canary), approved by payments-oncall", [a231])
payments_enact(9, 1, "16:13:00", "prod", "A231", [dec231, u4p], 4, (0, 3, 0, 1), minutes=22)
verify(T(9, 1, "16:50:00"), "payments/prod", "A231", "integration", "pass", "221 tests, 0 failures")
observed(T(9, 2, "08:00:00"), "platform/prod", {"network": "P12", "cluster": "P12"})
observed(T(9, 2, "08:00:10"), "payments/prod", {"payments": "A231"})


# ---------------------------------------------------------------------------
# Emit: sort by time (stable), assign ids, resolve refs.
# ---------------------------------------------------------------------------

def main():
    order = sorted(range(len(FACTS)), key=lambda i: (FACTS[i]["ts"], i))
    ids = {}
    for n, i in enumerate(order, 1):
        ids[id(FACTS[i])] = f"f{n:03d}"
    out = []
    for i in order:
        f = FACTS[i]
        row = {"id": ids[id(f)], "ts": f["ts"], "class": f["class"], "kind": f["kind"],
               "subject": f["subject"], "actor": f["actor"], "payload": f["payload"]}
        if f["rationale"]:
            row["rationale"] = f["rationale"]
        refs = [ids[id(r)] for r in f["refs"]]
        if refs:
            row["refs"] = refs
        out.append(json.dumps(row, separators=(",", ":")))
    path = os.path.join(HERE, "facts.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"wrote {len(out)} facts to {path}")


if __name__ == "__main__":
    main()
