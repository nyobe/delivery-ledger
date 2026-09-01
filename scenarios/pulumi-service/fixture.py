#!/usr/bin/env python3
"""The fixture, written as a timeline. Emits facts.jsonl.

    ./fixture.py            # rewrite facts.jsonl

The facts are authored here rather than typed into the JSONL by hand so that
one story can't drift into two: every timestamp is written once, every
policy's human-readable rule is derived from its structured terms, and
cross-references between facts are Python references resolved to ids after
the timeline is sorted. The output file is the ledger; this file is how it
was authored.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

FACTS = []


def T(day, hms):
    return f"2026-08-{day:02d}T{hms}Z"


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
# Gate terms: structured, evaluated by views.sql (v_gate_term). The `rule`
# text on the policy is rendered from these so the two can never disagree.
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


def rule_text(stage, terms):
    parts = []
    for t in terms:
        k = t["type"]
        if k == "verified":
            parts.append(f"verified({t['stage']}, F, '{t['check']}')")
        elif k == "carried":
            parts.append(f"carried({t['stage']}, F)")
        elif k == "approved":
            parts.append(f"approved({stage}, F, role = '{t['role']}')")
        elif k == "not_held":
            parts.append(f"NOT held({stage})")
        elif k == "plan_safe_or_approved":
            parts.append(f"(plan({stage}, F).safe OR approved({stage}, F, role = '{t['role']}'))")
    return " AND ".join(parts) or "true"


SAFE_RULE = "plan.delete = 0 AND plan.replace = 0 AND NOT plan.migrations_changed"

# ---------------------------------------------------------------------------
# Declarations — the delivery program applied 2026-08-12
# ---------------------------------------------------------------------------

VIA = "delivery program pulumi-service/delivery rev 3"
IMAGES = ["api", "console", "jobs", "ratelimit", "workflow"]

t0 = T(12, "15:00:00")
intent(t0, "warehouse.declared", "warehouse:master", "user:priya",
       {"program": "pulumi-service", "repo": "pulumi/pulumi-service", "branch": "master", "images": IMAGES,
        "stacks": ["service"], "config": "esc:pulumi/service (pinned version rides in the freight)", "via": VIA})

STAGES = [
    ("testing",       {"order": 1, "region": "us-west-2",    "owner": "internal-tools", "slack": "#ops-notif-testing",
                       "ops": "pulumi/pulumi-service/testing",       "upstream": "warehouse:master", "stacks": ["service"]}),
    ("testing-eu",    {"order": 2, "region": "eu-central-1", "owner": "internal-tools", "slack": "#ops-notif-testing",
                       "ops": "pulumi/pulumi-service/testing-eu",    "upstream": "warehouse:master", "stacks": ["service"]}),
    ("staging",       {"order": 3, "region": "us-west-2",    "owner": "internal-tools", "slack": "#ops-notif-staging",
                       "url": "https://app.pulumi-staging.io", "ops": "pulumi/pulumi-service/staging",
                       "upstream": "release-train", "stacks": ["service"]}),
    ("production",    {"order": 4, "region": "us-west-2",    "owner": "internal-tools", "slack": "#ops-alerts",
                       "url": "https://app.pulumi.com", "ops": "pulumi/pulumi-service/production",
                       "upstream": "staging", "stacks": ["service", "workflow-pool", "workflow-ami"]}),
    ("production-eu", {"order": 5, "region": "eu-central-1", "owner": "internal-tools", "slack": "#ops-alerts",
                       "ops": "pulumi/pulumi-service/production-eu",
                       "upstream": "production", "stacks": ["service", "workflow-pool", "workflow-ami"]}),
]
for i, (name, p) in enumerate(STAGES):
    intent(T(12, f"15:00:{i:02d}"), "stage.declared", f"stage:{name}", "user:priya",
           dict(p, display=name, program="pulumi-service", environment=name, via=VIA))

POLICIES = [
    ("testing", "auto", "freight.discovered on warehouse:master", [],
     "Every master build rolls to testing. Deployments as the degenerate single-stage pipeline."),
    ("testing-eu", "auto", "freight.discovered on warehouse:master", [],
     "Every master build rolls to testing-eu."),
    ("staging", "auto", "release.cut", [],
     "Opening the release PR deploys staging from it, unconditionally (pr-staging-deploy.yml)."),
    ("production", "gated", None,
     [verified("staging", "integration-tests"), verified("staging", "smoke"), verified("staging", "load-generator"),
      approved("oncall", via="merging the release PR into production")],
     "Oncall reviews staging and merges the release PR. The merge is the approval; nothing runs while it waits."),
    ("production-eu", "auto-if-safe", None,
     [carried("production"), verified("production", "integration-tests"), not_held(), plan_safe_or_approved("oncall")],
     "EU follows US automatically when the plan is boring. A migration or a destructive step requires oncall. "
     "(Proposed: today EU deploys in parallel with US.)"),
]
for i, (name, mode, trigger, terms, desc) in enumerate(POLICIES):
    p = {"mode": mode, "terms": terms, "rule": rule_text(name, terms), "description": desc}
    if trigger:
        p["trigger"] = trigger
    if any(t["type"] == "plan_safe_or_approved" for t in terms):
        p["safe"] = SAFE_RULE
    intent(T(12, f"15:00:{10 + i:02d}"), "policy.declared", f"stage:{name}", "user:priya", p)


def edge(consumer, producer, key):
    return f"edge:{consumer}<-{producer}.{key}"


E_AMI_US = edge("workflow-pool@production", "workflow-ami@production", "ami_id")
E_AMI_EU = edge("workflow-pool@production-eu", "workflow-ami@production-eu", "ami_id")
E_IMG_US = edge("workflow-pool@production", "service@production", "deploy_image_reference")

intent(T(12, "15:00:20"), "binding.declared", E_AMI_US, "user:priya",
       {"consumer": "workflow-pool@production", "producer": "workflow-ami@production", "key": "ami_id",
        "uptake": "gated", "description": "An instance refresh drains the hot pool. Approve the trigger; never auto-uptake."})
intent(T(12, "15:00:21"), "binding.declared", E_AMI_EU, "user:priya",
       {"consumer": "workflow-pool@production-eu", "producer": "workflow-ami@production-eu", "key": "ami_id",
        "uptake": "gated", "description": "Same as US: approve the trigger."})
intent(T(12, "15:00:22"), "binding.declared", E_IMG_US, "user:priya",
       {"consumer": "workflow-pool@production", "producer": "service@production", "key": "deploy_image_reference",
        "uptake": "auto", "description": "New deployment requests pick up whatever image reference the API advertises "
        "(PULUMI_DEPLOY_DEFAULT_IMAGE_REFERENCE). Nothing to approve; it ripples."})

# ---------------------------------------------------------------------------
# Freight and the pipeline, week by week
# ---------------------------------------------------------------------------

PEOPLE = {"jonas": "jonas", "amina": "amina", "priya": "priya", "maya": "maya"}


def pr(number, title, author):
    return {"number": number, "title": title, "author": author}


def discovered(ts, freight, sha, images, config, prs, build):
    return observe(ts, "freight.discovered", f"freight:{freight}", "ci:gha",
                   {"warehouse": "master", "source": {"repo": "pulumi/pulumi-service", "sha": sha, "branch": "master"},
                    "images": images, "config": {"service": config}, "prs": prs, "build": build})


def decide(ts, stage, freight, actor, rationale, refs=()):
    return intent(ts, "promotion.decided", f"stage:{stage}", actor, {"freight": freight}, rationale, refs)


def started(ts, tid, stage, freight, run, refs=(), stack="service", **extra):
    p = dict({"stage": stage, "freight": freight, "stack": stack, "run": run}, **extra)
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


def step(ts, tid, op, typ, urn, **extra):
    return observe(ts, "resource.step", f"transition:{tid}", "ci:gha", dict({"op": op, "type": typ, "urn": urn}, **extra))


def verify(ts, stage, freight, check, outcome, detail, actor="ci:gha", run=None):
    p = {"freight": freight, "check": check, "outcome": outcome, "detail": detail}
    if run:
        p["run"] = run
    return observe(ts, "verification.recorded", f"stage:{stage}", actor, p)


def plan(ts, stage, freight, against, counts, migrations=None, run=None, note=None):
    p = {"freight": freight, "against": against}
    p.update(dict(zip(("create", "update", "delete", "replace"), counts)))
    p["migrations_changed"] = bool(migrations)
    if migrations:
        p["migrations"] = migrations
    if run:
        p["run"] = run
    if note:
        p["note"] = note
    return observe(ts, "plan.summarized", f"stage:{stage}", "ci:gha", p)


def approve(ts, stage, freight, actor, via, rationale, refs=()):
    return intent(ts, "approval.granted", f"stage:{stage}", actor,
                  {"freight": freight, "role": "oncall", "via": via}, rationale, refs)


def job(ts, stage, freight, name, outcome, detail, optional=False):
    return observe(ts, "job.finished", f"stage:{stage}", "ci:gha",
                   {"freight": freight, "job": name, "outcome": outcome, "optional": optional, "detail": detail})


def publish(ts, producer, version, values, produced_by, **extra):
    return observe(ts, "output.published", f"record:{producer}", "ci:gha",
                   dict({"version": version, "values": values, "produced_by": produced_by}, **extra))


def uptake(ts, edge_subject, version, actor, rationale, refs=()):
    return intent(ts, "uptake.decided", edge_subject, actor, {"record_version": version}, rationale, refs)


def observed(ts, stage, services):
    return observe(ts, "state.observed", f"stage:{stage}", "watch:ecs",
                   {"stack": "service", "services": services, "source": "ecs describe-services, task-definition SHA tags"})


def release_cut(ts, freight, number, branch):
    return intent(ts, "release.cut", f"freight:{freight}", "cron:release-15utc",
                  {"release_pr": number, "branch": branch, "title": f"release@{branch.split('/')[1]}", "label": "release"},
                  "weekday release cron (15:00 UTC)")


def master_push(day, hms, freight, sha, images, config, prs, build, ops):
    """A master build: discovered, and auto-rolled to both testing stages."""
    d = discovered(T(day, hms), freight, sha, images, config, prs, build)
    h, m, s = hms.split(":")
    t1 = T(day, f"{h}:{int(m) + 1:02d}:00")
    a = decide(t1, "testing", freight, "policy:testing", "auto: master build discovered", [d])
    b = decide(T(day, f"{h}:{int(m) + 1:02d}:01"), "testing-eu", freight, "policy:testing-eu", "auto: master build discovered", [d])
    started(T(day, f"{h}:{int(m) + 1:02d}:30"), f"{freight}-testing", "testing", freight, build, [a])
    started(T(day, f"{h}:{int(m) + 1:02d}:35"), f"{freight}-testing-eu", "testing-eu", freight, build, [b])
    return d, ops


# --- F416: last week's release (baseline) -----------------------------------
IMG416 = {"api": "sha256:16a1e3", "console": "sha256:9b2c77", "jobs": "sha256:0d4f10", "ratelimit": "sha256:5e6a22", "workflow": "sha256:c1d2e3"}
d416, _ = master_push(17, "22:03:00", "F416", "3d9f1a7", IMG416, "v41",
                      [pr(46101, "Jobs: retry webhook deliveries with backoff", "jonas"),
                       pr(46098, "Console: new stack list empty state", "amina")], "gha:push-master/7712", None)
finished(T(17, "22:25:00"), "F416-testing-eu", "succeeded", 5108, (1, 9, 0, 0))
finished(T(17, "22:27:00"), "F416-testing", "succeeded", 6197, (1, 9, 0, 0))
verify(T(17, "22:40:00"), "testing", "F416", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-master/7712")

c416 = release_cut(T(18, "15:00:00"), "F416", 14870, "release/2026-08-18--0800")
s416 = decide(T(18, "15:01:00"), "staging", "F416", "policy:staging", "release PR #14870 opened", [c416])
started(T(18, "15:01:30"), "F416-staging", "staging", "F416", "gha:pr-staging-deploy/7740", [s416])
finished(T(18, "15:18:00"), "F416-staging", "succeeded", 1409, (1, 9, 0, 0))
v1 = verify(T(18, "15:31:00"), "staging", "F416", "integration-tests", "pass", "412 tests, 0 failures", run="gha:pr-staging-deploy/7740")
v2 = verify(T(18, "15:33:00"), "staging", "F416", "smoke", "pass", "deploy-smoke-test green", run="gha:deploy-smoke-test/2210")
v3 = verify(T(18, "15:40:00"), "staging", "F416", "load-generator", "pass", "100% success over 10m", actor="watch:load-generator")
plan(T(18, "15:42:00"), "production", "F416", "F415", (1, 9, 0, 0), run="gha:pr-staging-deploy/7740")
a416 = approve(T(18, "16:10:00"), "production", "F416", "user:maya", "merged release PR #14870",
               "staging clean; load-generator 100%; no migrations", [v1, v2, v3])
p416 = decide(T(18, "16:10:05"), "production", "F416", "policy:production",
              "gate satisfied: verified in staging (integration, smoke, load-generator) and approved by oncall", [a416, v1, v2, v3])
started(T(18, "16:11:00"), "F416-prod", "production", "F416", "gha:push-build/7742", [p416])
f416p = finished(T(18, "16:34:00"), "F416-prod", "succeeded", 3171, (1, 9, 0, 0))
pub71 = publish(T(18, "16:34:30"), "service@production", 71,
                {"deploy_image_reference": "pulumi/pulumi-deploy@sha256:c1d2e3"}, "F416-prod (stack outputs)")
u71 = uptake(T(18, "16:34:35"), E_IMG_US, 71, "policy:uptake", "auto: binding declares auto uptake; new image reference published", [pub71])
started(T(18, "16:35:00"), "pool-prod-img71", "production", None, "gha:update-stack/7743", [u71], stack="workflow-pool", record_version=71)
finished(T(18, "16:38:00"), "pool-prod-img71", "succeeded", 897, (0, 1, 0, 0), detail="worker launch config advertises the new default image")
v4 = verify(T(18, "16:46:00"), "production", "F416", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-build/7742")
pl416eu = plan(T(18, "16:47:00"), "production-eu", "F416", "F415", (1, 9, 0, 0), run="gha:push-build/7742")
e416 = decide(T(18, "16:47:30"), "production-eu", "F416", "policy:production-eu",
              "auto: production carries F416 and verified it; plan is safe (0 deletes, 0 replaces, no migrations)", [f416p, v4, pl416eu])
started(T(18, "16:48:00"), "F416-prod-eu", "production-eu", "F416", "gha:push-build/7742", [e416])
finished(T(18, "17:05:00"), "F416-prod-eu", "succeeded", 2064, (1, 9, 0, 0))
verify(T(18, "17:16:00"), "production-eu", "F416", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-build/7742")

# --- the AMI edge: weekly patch, gated uptake ---------------------------------
pub12 = publish(T(19, "08:00:00"), "workflow-ami@production", 12, {"ami_id": "ami-0a17c4d2e9b3f5a61"}, "workflow-ami bake #87", base="al2023-2026.08.17")
pub9 = publish(T(19, "08:10:00"), "workflow-ami@production-eu", 9, {"ami_id": "ami-0c58e1f7a2d4b9c03"}, "workflow-ami-eu bake #61", base="al2023-2026.08.17")
u12 = uptake(T(19, "09:30:00"), E_AMI_US, 12, "user:priya", "weekly AMI patch; refresh during low traffic", [pub12])
started(T(19, "09:31:00"), "pool-prod-ami12", "production", None, "gha:update-stack/7751", [u12], stack="workflow-pool", record_version=12)
fpool = finished(T(19, "10:02:00"), "pool-prod-ami12", "succeeded", 902, (0, 2, 0, 0), detail="launch template v13; instance refresh completed")
u9 = uptake(T(19, "10:05:00"), E_AMI_EU, 9, "user:priya", "weekly AMI patch; US refresh clean", [pub9, fpool])
started(T(19, "10:06:00"), "pool-prod-eu-ami9", "production-eu", None, "gha:update-stack/7752", [u9], stack="workflow-pool", record_version=9)
finished(T(19, "10:31:00"), "pool-prod-eu-ami9", "succeeded", 611, (0, 2, 0, 0))

# --- F417: Monday's release, with a migration, then an incident ------------------
IMG417 = {"api": "sha256:2f8b91", "console": "sha256:9b2c77", "jobs": "sha256:71ac05", "ratelimit": "sha256:5e6a22", "workflow": "sha256:d4e5f6"}
d417, _ = master_push(21, "21:40:00", "F417", "7c9e2b4", IMG417, "v41",
                      [pr(46120, "Migrate stack tags to new table", "jonas"),
                       pr(46133, "Tighten rate limits on /api/stacks", "amina"),
                       pr(46140, "Bump workflow image to 2026.08.21", "priya")], "gha:push-master/7788", None)
finished(T(21, "22:03:00"), "F417-testing-eu", "succeeded", 5120, (3, 11, 0, 0))
finished(T(21, "22:05:00"), "F417-testing", "succeeded", 6210, (3, 11, 0, 0))
verify(T(21, "22:17:00"), "testing", "F417", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-master/7788")

c417 = release_cut(T(24, "15:00:00"), "F417", 14876, "release/2026-08-24--0800")
s417 = decide(T(24, "15:01:00"), "staging", "F417", "policy:staging", "release PR #14876 opened", [c417])
started(T(24, "15:01:30"), "F417-staging", "staging", "F417", "gha:pr-staging-deploy/7801", [s417])
finished(T(24, "15:18:00"), "F417-staging", "succeeded", 1415, (3, 11, 0, 0))
v1 = verify(T(24, "15:30:00"), "staging", "F417", "integration-tests", "pass", "412 tests, 0 failures", run="gha:pr-staging-deploy/7801")
v2 = verify(T(24, "15:32:00"), "staging", "F417", "smoke", "pass", "deploy-smoke-test green", run="gha:deploy-smoke-test/2231")
v3 = verify(T(24, "15:38:00"), "staging", "F417", "load-generator", "pass", "100% success over 10m", actor="watch:load-generator")
pl417 = plan(T(24, "15:40:00"), "production", "F417", "F416", (3, 11, 0, 0), migrations=["20260821_stack_tags.sql"], run="gha:pr-staging-deploy/7801")
a417 = approve(T(24, "16:05:00"), "production", "F417", "user:maya", "merged release PR #14876",
               "staging clean; the stack-tags migration is additive (new table, no drops)", [v1, v2, v3, pl417])
p417 = decide(T(24, "16:05:05"), "production", "F417", "policy:production",
              "gate satisfied: verified in staging (integration, smoke, load-generator) and approved by oncall", [a417, v1, v2, v3])
started(T(24, "16:06:00"), "F417-prod", "production", "F417", "gha:push-build/7803", [p417], strategy="ecs-rolling (create before delete)")
phase(T(24, "16:06:30"), "F417-prod", "provisioning", "new task definitions registered; new tasks starting alongside old")
phase(T(24, "16:19:00"), "F417-prod", "both-live", "old and new tasks serving; ALB health checks passing on new")
phase(T(24, "16:24:00"), "F417-prod", "cutover", "target groups shifted to new tasks")
phase(T(24, "16:25:00"), "F417-prod", "retiring", "draining old tasks")
f417p = finished(T(24, "16:31:00"), "F417-prod", "succeeded", 3184, (3, 11, 0, 0))
pub72 = publish(T(24, "16:31:30"), "service@production", 72,
                {"deploy_image_reference": "pulumi/pulumi-deploy@sha256:d4e5f6"}, "F417-prod (stack outputs)")
u72 = uptake(T(24, "16:31:35"), E_IMG_US, 72, "policy:uptake", "auto: binding declares auto uptake; new image reference published", [pub72])
started(T(24, "16:32:00"), "pool-prod-img72", "production", None, "gha:update-stack/7804", [u72], stack="workflow-pool", record_version=72)
finished(T(24, "16:35:00"), "pool-prod-img72", "succeeded", 903, (0, 1, 0, 0))
job(T(24, "16:33:00"), "production", "F417", "sentry-release", "succeeded", "release marked in Sentry")
job(T(24, "16:34:00"), "production", "F417", "notify-deployed-prs", "failed", "GitHub rate-limited; nothing downstream reads this", optional=True)
v4 = verify(T(24, "16:44:00"), "production", "F417", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-build/7803")
pl417eu = plan(T(24, "16:45:00"), "production-eu", "F417", "F416", (3, 11, 0, 0), migrations=["20260821_stack_tags.sql"], run="gha:push-build/7803")
a417eu = approve(T(24, "17:02:00"), "production-eu", "F417", "user:maya", "pulumi delivery approve production-eu F417",
                 "plan not auto-safe (migration); migration is additive and US has been healthy for 30m", [pl417eu, v4])
e417 = decide(T(24, "17:02:05"), "production-eu", "F417", "policy:production-eu",
              "gate satisfied: production carries and verified F417; plan not safe, oncall approved", [f417p, v4, pl417eu, a417eu])
started(T(24, "17:03:00"), "F417-prod-eu", "production-eu", "F417", "gha:push-build/7803", [e417])
finished(T(24, "17:19:00"), "F417-prod-eu", "succeeded", 2071, (3, 11, 0, 0))
verify(T(24, "17:30:00"), "production-eu", "F417", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-build/7803")

# The incident: the side door, and the watch that sees it.
intent(T(24, "23:40:00"), "breakglass.recorded", "stage:production", "user:maya",
       {"scope": "service:api", "action": "ECS force-deploy previous task definition", "from_freight": "F417",
        "to_freight": "F416", "incident": "INC-2311", "expiry": "until the next promotion to production"},
       "5xx spike on /api/stacks after #46133 (rate-limit key collision). Rolled api back to the F416 task definition "
       "via the ECS console; console/jobs/ratelimit/workflow left at F417. The stack-tags migration is additive, so old "
       "api code is safe against the new schema.", [f417p])
observed(T(24, "23:52:00"), "production", {"api": "F416", "console": "F417", "jobs": "F417", "ratelimit": "F417", "workflow": "F417"})

# --- F418: the fix, cut Wednesday, waiting on oncall ------------------------------
IMG418 = {"api": "sha256:8a7b6c", "console": "sha256:9b2c77", "jobs": "sha256:71ac05", "ratelimit": "sha256:33e4d1", "workflow": "sha256:d4e5f6"}
d418, _ = master_push(25, "22:14:00", "F418", "a1b2c3d", IMG418, "v42",
                      [pr(46173, "Fix rate limiter key collision (INC-2311)", "amina"),
                       pr(46170, "Add index for stack tags lookup", "jonas"),
                       pr(46168, "Bump pulumi CLI to 3.212.0", "priya")], "gha:push-master/7830", None)
finished(T(25, "22:39:00"), "F418-testing-eu", "succeeded", 5133, (2, 14, 0, 0))
finished(T(25, "22:41:00"), "F418-testing", "succeeded", 6228, (2, 14, 0, 0))
verify(T(25, "22:55:00"), "testing", "F418", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-master/7830")

publish(T(26, "07:30:00"), "workflow-ami@production", 13, {"ami_id": "ami-0f3c9e8b7a6d5c412"}, "workflow-ami bake #88", base="al2023-2026.08.24")
intent(T(26, "14:50:00"), "hold.placed", "stage:production-eu", "user:priya",
       {"until": T(26, "20:00:00"), "scope": "promotions"},
       "EU maintenance window: RDS minor version upgrade in progress; no rollouts until 20:00 UTC")

c418 = release_cut(T(26, "15:00:00"), "F418", 14882, "release/2026-08-26--0800")
s418 = decide(T(26, "15:01:00"), "staging", "F418", "policy:staging", "release PR #14882 opened", [c418])
started(T(26, "15:01:30"), "F418-staging", "staging", "F418", "gha:pr-staging-deploy/7841", [s418])
step(T(26, "15:05:00"), "F418-staging", "create", "aws:ecs/taskDefinition:TaskDefinition", "urn:pulumi:staging::pulumi-service::aws:ecs/taskDefinition:TaskDefinition::api")
step(T(26, "15:06:00"), "F418-staging", "update", "aws:ecs/service:Service", "urn:pulumi:staging::pulumi-service::aws:ecs/service:Service::api")
step(T(26, "15:12:00"), "F418-staging", "update", "aws:ecs/service:Service", "urn:pulumi:staging::pulumi-service::aws:ecs/service:Service::console")
finished(T(26, "15:17:00"), "F418-staging", "succeeded", 1421, (5, 12, 0, 0))
job(T(26, "15:18:00"), "staging", "F418", "sentry-release", "succeeded", "release marked in Sentry")
verify(T(26, "15:29:00"), "staging", "F418", "integration-tests", "pass", "412 tests, 0 failures", run="gha:pr-staging-deploy/7841")
verify(T(26, "15:31:00"), "staging", "F418", "smoke", "pass", "deploy-smoke-test green", run="gha:deploy-smoke-test/2244")
verify(T(26, "15:35:00"), "staging", "F418", "load-generator", "pass", "100% success over 10m", actor="watch:load-generator")
plan(T(26, "15:36:00"), "production", "F418", "world@2026-08-26T15:36Z (api at F416 task def, rest at F417)", (2, 14, 0, 0),
     migrations=["20260825_stack_tags_index.sql"], run="gha:pr-staging-deploy/7841")
plan(T(26, "15:40:00"), "production-eu", "F418", "F417", (2, 14, 0, 0), migrations=["20260825_stack_tags_index.sql"],
     run="gha:pr-staging-deploy/7841", note="computed ahead of candidacy: the gate is evaluable at rest")

# --- Wednesday afternoon on master: a failure, then a supersession -----------------
IMG419 = dict(IMG418, console="sha256:4c1d90")
d419, _ = master_push(26, "16:02:00", "F419", "e5f6a7b", IMG419, "v42",
                      [pr(46181, "Console: fix stack list pagination", "jonas")], "gha:push-master/7846", None)
step(T(26, "16:08:00"), "F419-testing-eu", "update", "aws:ecs/service:Service",
     "urn:pulumi:testing-eu::pulumi-service::aws:ecs/service:Service::console",
     error="deployment circuit breaker triggered: 3 tasks failed ELB health checks")
finished(T(26, "16:09:00"), "F419-testing-eu", "failed", 5134, (0, 0, 0, 0),
         error="aws:ecs/service:Service console: deployment circuit breaker triggered; rolled back to previous task set",
         failed_step="Update `testing-eu`", step_url="gha:push-master/7846#step:9:1")

IMG420 = dict(IMG419, console="sha256:5d2e01")
d420, _ = master_push(26, "16:20:00", "F420", "f7a8b9c", IMG420, "v42",
                      [pr(46184, "Console: guard empty page in stack list pagination", "jonas")], "gha:push-master/7851", None)
# F420's decision supersedes the in-flight F419 rollout on testing: abandoned, incumbent untouched.
dec420 = [f for f in FACTS if f["kind"] == "promotion.decided" and f["subject"] == "stage:testing" and f["payload"]["freight"] == "F420"][0]
finished(T(26, "16:21:15"), "F419-testing", "abandoned", None, None, refs=[dec420],
         detail="superseded by the decision to carry F420; incumbent F418 untouched")
finished(T(26, "16:48:00"), "F420-testing", "succeeded", 6231, (0, 1, 0, 0))
finished(T(26, "16:49:00"), "F420-testing-eu", "succeeded", 5135, (0, 1, 0, 0))
verify(T(26, "17:01:00"), "testing", "F420", "integration-tests", "pass", "412 tests, 0 failures", run="gha:push-master/7851")

# Conformance reads on cadence: production still drifted, EU matches.
observed(T(26, "17:30:00"), "production", {"api": "F416", "console": "F417", "jobs": "F417", "ratelimit": "F417", "workflow": "F417"})
observed(T(26, "17:30:10"), "production-eu", {"api": "F417", "console": "F417", "jobs": "F417", "ratelimit": "F417", "workflow": "F417"})

# F420's enactments start at 16:21:30 / 16:21:35 (master_push), after the abandonment above, so testing never
# has two transitions in flight at once.


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
