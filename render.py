#!/usr/bin/env python3
"""facts.jsonl → sqlite → views → the tracker, as one HTML page.

    ./render.py                                   lint, self-check, render out/index.html
    ./render.py --lint-only                       lint + self-checks, no HTML
    ./render.py --as-of 2026-08-24T10:20:00Z      render the ledger as it stood then

Everything the page shows is a SELECT over the `facts` table (schema.sql,
views.sql). The renderer holds no state: it loads the facts whose timestamp is
at or before `--as-of`, sets the clock, and formats query results. Rendering
the same ledger at several instants is the cheapest proof that the ledger, not
the UI, is where state lives — the page carries a snapshot switcher for that.
"""
import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

INTENT_KINDS = {
    "stage.declared", "policy.declared", "binding.declared", "release.cut",
    "promotion.decided", "approval.granted", "hold.placed", "breakglass.recorded",
    "uptake.decided",
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
SUBJECT_TYPES = {"stage", "freight", "transition", "edge", "record"}
# Derived states must never be written down. If a fact carries one of these,
# the demo is storing what it claims to compute.
FORBIDDEN_KEYS = {"status", "state"}
FORBIDDEN_VALUES = {"awaiting", "drifted", "converged", "held", "pending", "ready", "in-flight"}

DEFAULT_AS_OF = "2026-08-26T11:45:00Z"
SNAPSHOTS = [
    # (as_of, caption) — the primary first; the rest are the time-travel tabs.
    (DEFAULT_AS_OF, "Wed 11:45 — F418 waiting on oncall; production drifted since Monday night"),
    ("2026-08-24T10:20:00Z", "Mon 10:20 — F417 mid-rollout to production, both versions live"),
    ("2026-08-24T10:50:00Z", "Mon 10:50 — production-eu holding F417 for approval: the plan touches a migration"),
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
                raise SystemExit(f"facts.jsonl:{n}: not JSON: {e}")
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
    declared_stages, discovered_freight, declared_edges = set(), set(), set()
    transitions = {}  # subject -> {"started": bool, "finished": int}

    for i, f in enumerate(facts, 1):
        for k in ("id", "ts", "class", "kind", "subject", "actor", "payload"):
            if k not in f:
                err(i, f, f"missing {k}")
        if errors:
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
                err(i, f, f"ref {r} does not point at an earlier fact")

        # Subject bookkeeping: declare before use.
        if kind == "stage.declared":
            declared_stages.add(sname)
        elif kind == "freight.discovered":
            discovered_freight.add(sname)
        elif kind == "binding.declared":
            declared_edges.add(subj)

        if stype == "stage" and kind != "stage.declared" and sname not in declared_stages:
            err(i, f, f"stage {sname} used before declaration")
        if stype == "freight" and kind != "freight.discovered" and sname not in discovered_freight:
            err(i, f, f"freight {sname} used before discovery")
        if stype == "edge" and kind != "binding.declared" and subj not in declared_edges:
            err(i, f, "edge used before its binding was declared")
        for key in ("stage",):
            if key in p and p[key] not in declared_stages:
                err(i, f, f"payload.{key} {p[key]} not a declared stage")
        for key in ("freight", "from_freight", "to_freight"):
            if key in p and p[key] not in discovered_freight:
                err(i, f, f"payload.{key} {p[key]} not a discovered freight")

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
# negative cases so a broken mechanism cannot stay green.
# ---------------------------------------------------------------------------

def self_checks(facts):
    checks = []

    def check(name, as_of, fn):
        checks.append((name, as_of, fn))

    # --- at the primary instant --------------------------------------------
    A = DEFAULT_AS_OF
    check("grid: production is awaiting oncall approval, not failed", A,
          lambda db: (lambda r: r["status"] == "awaiting" and r["awaiting"].startswith("approval")
                      and r["candidate"] == "F418" and r["desired"] == "F417")
          (one(db, "SELECT * FROM v_grid WHERE stage='production'")))
    check("grid: production awaiting_since is the last verification, not the cut", A,
          lambda db: one(db, "SELECT * FROM v_grid WHERE stage='production'")["awaiting_since"] == "2026-08-26T09:35:00Z")
    check("grid: production drift is explained by the break-glass fact", A,
          lambda db: "INC-2311" in (one(db, "SELECT * FROM v_grid WHERE stage='production'")["drift"] or ""))
    check("grid: production-eu is converged (candidate == desired) and held", A,
          lambda db: (lambda r: r["status"] == "converged" and r["hold_until"] is not None and r["drift"] is None)
          (one(db, "SELECT * FROM v_grid WHERE stage='production-eu'")))
    check("grid: staging converged on F418; testing converged on F419", A,
          lambda db: one(db, "SELECT carried FROM v_grid WHERE stage='staging'")["carried"] == "F418"
          and one(db, "SELECT carried, status FROM v_grid WHERE stage='testing'")["carried"] == "F419")
    check("grid: no stage is in-flight or failed", A,
          lambda db: q(db, "SELECT stage FROM v_grid WHERE status IN ('in-flight','failed')") == [])
    check("lanes: F418 has not reached production and is the awaiting freight there", A,
          lambda db: (lambda r: r["reached_at"] is None and r["awaiting"] is not None)
          (one(db, "SELECT * FROM v_lanes WHERE freight='F418' AND stage='production'")))
    check("lanes: F417 is NOT awaiting anywhere (it is desired everywhere it sits)", A,
          lambda db: q(db, "SELECT * FROM v_lanes WHERE freight='F417' AND awaiting IS NOT NULL") == [])
    check("gate: production-eu × F416 was auto-safe; × F417 needed and got approval; × F418 would need approval", A,
          lambda db: one(db, "SELECT plan_safe FROM v_gate_all WHERE stage='production-eu' AND freight='F416'")["plan_safe"] == 1
          and (lambda r: r["plan_safe"] == 0 and r["approved_at"] is not None)
          (one(db, "SELECT * FROM v_gate_all WHERE stage='production-eu' AND freight='F417'"))
          and (lambda r: r["plan_safe"] == 0 and r["approved_at"] is None and r["upstream_carried_at"] is None)
          (one(db, "SELECT * FROM v_gate_all WHERE stage='production-eu' AND freight='F418'")))
    check("gate: an approval on production does not count for production-eu", A,
          lambda db: one(db, "SELECT approved_at FROM v_gate_all WHERE stage='production-eu' AND freight='F416'")["approved_at"] is None)
    check("uptake: production edge pending (v13 > v12); production-eu edge not pending (v9 == v9)", A,
          lambda db: one(db, "SELECT pending FROM v_pending_uptake WHERE consumer='workflow-pool@production'")["pending"] == 1
          and one(db, "SELECT pending FROM v_pending_uptake WHERE consumer='workflow-pool@production-eu'")["pending"] == 0)
    check("trace: #46181 is in the testing stages only and in no release", A,
          lambda db: (lambda r: r["furthest_stage"] in ("testing", "testing-eu") and r["release_pr"] is None)
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=46181"))
          and q(db, "SELECT * FROM v_trace WHERE pr=46181 AND reached_at IS NOT NULL AND stage NOT IN ('testing','testing-eu')") == [])
    check("trace: #46173 reached staging, next is production approval", A,
          lambda db: (lambda r: r["furthest_stage"] == "staging" and r["next"].startswith("production: approval"))
          (one(db, "SELECT * FROM v_trace_summary WHERE pr=46173")))
    check("trace: #46120 shipped everywhere (production-eu)", A,
          lambda db: one(db, "SELECT * FROM v_trace_summary WHERE pr=46120")["furthest_stage"] == "production-eu")
    check("observed: production has exactly one mismatched service (api@F416)", A,
          lambda db: (lambda r: r["mismatches"] == 1 and r["mismatch_detail"] == "api@F416")
          (one(db, "SELECT * FROM v_observed WHERE stage='production'")))
    check("observed: production-eu matches intent", A,
          lambda db: one(db, "SELECT mismatches FROM v_observed WHERE stage='production-eu'")["mismatches"] == 0)
    check("releases: three cut, in cut order, F418 not yet in production", A,
          lambda db: [r["freight"] for r in q(db, "SELECT freight FROM v_releases")] == ["F418", "F417", "F416"]
          and one(db, "SELECT production_at FROM v_releases WHERE freight='F418'")["production_at"] is None)

    # --- time travel: Monday mid-rollout ------------------------------------
    B = "2026-08-24T10:20:00Z"
    check("as of Mon 10:20: production in-flight, phase both-live, carried still F416", B,
          lambda db: (lambda r: r["status"] == "in-flight" and r["last_phase"] == "both-live" and r["carried"] == "F416")
          (one(db, "SELECT * FROM v_grid WHERE stage='production'")))
    check("as of Mon 10:20: no break-glass, no drift, nothing held", B,
          lambda db: q(db, "SELECT stage FROM v_grid WHERE drift IS NOT NULL OR hold_until IS NOT NULL") == [])
    check("as of Mon 10:20: F418 does not exist yet", B,
          lambda db: q(db, "SELECT * FROM v_freight WHERE freight='F418'") == [])

    # --- time travel: Monday, the EU diff-gate live ---------------------------
    C = "2026-08-24T10:50:00Z"
    check("as of Mon 10:50: production-eu awaiting approval because the plan is not safe", C,
          lambda db: (lambda r: r["status"] == "awaiting" and "not safe" in r["awaiting"] and r["candidate"] == "F417")
          (one(db, "SELECT * FROM v_grid WHERE stage='production-eu'")))
    check("as of Mon 10:50: production converged on F417 (rollout done, not yet drifted)", C,
          lambda db: (lambda r: r["status"] == "converged" and r["carried"] == "F417" and r["drift"] is None)
          (one(db, "SELECT * FROM v_grid WHERE stage='production'")))

    # --- time travel: thirty seconds after Wednesday's cut ---------------------
    D = "2026-08-26T09:00:30Z"
    check("as of Wed 09:00:30: staging's gate passes on testing's verification — status ready, not awaiting", D,
          lambda db: (lambda r: r["status"] == "ready" and r["candidate"] == "F418" and r["desired"] == "F417")
          (one(db, "SELECT * FROM v_grid WHERE stage='staging'")))
    check("as of Wed 09:00:30: production's candidate is still F417 (staging has not carried F418)", D,
          lambda db: one(db, "SELECT candidate FROM v_grid WHERE stage='production'")["candidate"] == "F417")

    failures = []
    dbs = {}
    for name, as_of, fn in checks:
        db = dbs.get(as_of) or dbs.setdefault(as_of, build_db(facts, as_of))
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
    d = parse_ts(ts)
    return d.strftime("%a %b %-d %H:%M")


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
    return f'<a class="fact" href="#{esc(fid)}">{esc(fid)}</a>' if fid else ""


GLYPH = {
    "converged": ("●", "ok"), "awaiting": ("○", "amber"), "held": ("⊘", "hold"),
    "in-flight": ("◌", "live"), "failed": ("✕", "bad"), "ready": ("▸", "ok"),
    "pending": ("○", "hold"),
}


def status_chip(status):
    g, cls = GLYPH.get(status, ("·", "hold"))
    return f'<span class="chip {cls}"><span class="glyph">{g}</span>{esc(status)}</span>'


def class_chip(cls):
    return f'<span class="cls {esc(cls)}">{esc(cls)}</span>'


def table(cols, rows, cls=""):
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    if not rows:
        body = f'<tr><td colspan="{len(cols)}" class="empty">nothing — the ledger has no rows for this yet</td></tr>'
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
# Screens
# ---------------------------------------------------------------------------

def screen_grid(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_grid"):
        detail = ""
        if r["status"] == "awaiting":
            detail = f'{esc(r["awaiting"])} · <b>{esc(fmt_since(r["awaiting_since"], as_of))}</b> (since {esc(fmt_ts(r["awaiting_since"]))})'
        elif r["status"] == "in-flight":
            detail = f'{mono(r["inflight"])} · phase <b>{esc(r["last_phase"] or "starting")}</b>'
        elif r["status"] == "held":
            detail = f'held until {esc(fmt_ts(r["held_until"]))}'
        hold = ""
        if r["hold_until"]:
            hold = (f'<div class="note"><span class="chip hold"><span class="glyph">⊘</span>hold</span> '
                    f'until {esc(fmt_ts(r["hold_until"]))} by {mono(r["hold_by"])} — {esc(r["hold_rationale"])}</div>')
        drift = f'<div class="note drift"><span class="glyph">⚠</span> observed {esc(r["drift"])} · read {esc(fmt_ts(r["observed_at"]))}</div>' if r["drift"] else ""
        candidate = ""
        if r["candidate"] and r["candidate"] != r["desired"]:
            candidate = f' <span class="muted">→ {esc(r["candidate"])} offered</span>'
        rows.append([
            f'<b>{esc(r["stage"])}</b><div class="muted small">{esc(r["region"])} · {esc(r["owner"])}</div>',
            status_chip(r["status"]) + (f'<div class="small">{detail}</div>' if detail else "") + hold + drift,
            f'{mono(r["desired"])}{candidate}<div class="muted small">{esc(fmt_ts(r["decided_at"]))} · {esc(r["decided_by"])}</div>',
            f'{mono(r["carried"])}<div class="muted small">since {esc(fmt_ts(r["carried_since"]))} · {esc(fmt_since(r["carried_since"], as_of))} · update #{esc(r["ops_update"])}</div>',
        ])
    body = table(["stage", "state (derived)", "should carry (intent)", "carries (observed)"], rows, "grid")
    return section("grid", "1 · the subject grid", "What is where, since when, waiting on whom",
                   "One row per stage. <em>Should carry</em> is the latest promotion decision; <em>carries</em> is the "
                   "latest enactment that finished; the state column is a join of the two with the gate evaluation, "
                   "the active holds, and the last conformance read. Nothing in the state column is stored.",
                   body, sql_block(db, "v_grid", "v_gate_eval", "v_candidate"))


def screen_lanes(db, as_of):
    stages = [r["stage"] for r in q(db, "SELECT stage FROM v_stage ORDER BY ord")]
    freights = q(db, "SELECT DISTINCT freight, release_pr, cut_at, discovered_at FROM v_lanes ORDER BY discovered_at DESC")
    lanes = {(r["freight"], r["stage"]): r for r in q(db, "SELECT * FROM v_lanes")}
    rows = []
    for fr in freights:
        label = f'<b>{esc(fr["freight"])}</b>'
        label += f'<div class="muted small">release PR #{esc(fr["release_pr"])} · cut {esc(fmt_ts(fr["cut_at"]))}</div>' if fr["release_pr"] else \
                 f'<div class="muted small">master build · {esc(fmt_ts(fr["discovered_at"]))} · no release yet</div>'
        cells = [label]
        for s in stages:
            c = lanes[(fr["freight"], s)]
            if c["reached_at"]:
                cur = ' <span class="chip ok tiny"><span class="glyph">●</span>current</span>' if c["is_current"] else ""
                cells.append(f'<span class="reached">{esc(fmt_ts(c["reached_at"]))}</span>{cur}')
            elif c["inflight_since"]:
                cells.append(f'<span class="chip live"><span class="glyph">◌</span>in flight</span><div class="small muted">since {esc(fmt_ts(c["inflight_since"]))}</div>')
            elif c["awaiting"]:
                cells.append(f'<span class="chip amber"><span class="glyph">○</span>awaiting</span><div class="small">{esc(c["awaiting"])}<br><span class="muted">{esc(fmt_since(c["awaiting_since"], as_of))}</span></div>')
            else:
                cells.append('<span class="muted">—</span>')
        rows.append(cells)
    body = table(["freight"] + stages, rows, "lanes")
    return section("lanes", "1 · freight lanes", "Freight × stage",
                   "Each freight runs vertically through the stages it has reached. A cell is the time the enactment "
                   "finished there, or what the freight is waiting on to get there. The same content-keyed freight "
                   "moves through every stage — the testing build <em>is</em> the release.",
                   body, sql_block(db, "v_lanes"))


def screen_awaiting(db, as_of):
    g = one(db, "SELECT * FROM v_gate_eval WHERE stage='production'")
    p = one(db, "SELECT * FROM v_policy WHERE stage='production'")
    if not g:
        body = '<p class="muted">production has no candidate at this instant.</p>'
        return section("awaiting", "2 · awaiting ≠ failure", "Production, right now", "", body, sql_block(db, "v_gate_eval"))
    terms = [
        ("verified in staging: integration-tests", g["upstream_integration_at"],
         one(db, "SELECT fact, detail FROM v_verified WHERE stage='staging' AND freight=? AND chk='integration-tests'", g["freight"])),
        ("verified in staging: smoke", g["staging_smoke_at"],
         one(db, "SELECT fact, detail FROM v_verified WHERE stage='staging' AND freight=? AND chk='smoke'", g["freight"])),
        ("verified in staging: load-generator", g["staging_loadgen_at"],
         one(db, "SELECT fact, detail FROM v_verified WHERE stage='staging' AND freight=? AND chk='load-generator'", g["freight"])),
        ("approved by oncall (merge of the release PR)", g["approved_at"],
         {"fact": g["approval_fact"], "detail": g["approved_by"]} if g["approved_at"] else None),
    ]
    rows = []
    for name, at, ev in terms:
        if at:
            rows.append([f'<span class="chip ok"><span class="glyph">✓</span>met</span>', esc(name), esc(fmt_ts(at)),
                         (esc(ev["detail"]) + " " + fact_ref(ev["fact"])) if ev else ""])
        else:
            rows.append([f'<span class="chip amber"><span class="glyph">○</span>open</span>', esc(name),
                         f'<span class="muted">— waiting {esc(fmt_since(g["awaiting_since"], as_of))}</span>', "nothing running while it waits"])
    verdict = ("passes" if g["passes"] else f'does not pass — awaiting <b>{esc(g["awaiting"])}</b> since {esc(fmt_ts(g["awaiting_since"]))} ({esc(fmt_since(g["awaiting_since"], as_of))})')
    body = (f'<p class="lede">Candidate <code>{esc(g["freight"])}</code> for <b>production</b> (policy: {esc(p["mode"])}). '
            f'Gate <code>{esc(p["rule"])}</code> {verdict}.</p>'
            + table(["", "requirement", "satisfied at", "evidence"], rows))
    return section("awaiting", "2 · awaiting ≠ failure", "Production is waiting, not broken",
                   "The gate is a query joining both fact classes, evaluated at rest. What it lacks is an approval "
                   "fact; until one arrives the stage is <em>awaiting</em>, rendered as an honest open item with a "
                   "duration — not as a failed run. Suspending holds no compute: there is no runner parked on this.",
                   body, sql_block(db, "v_gate_all"))


def screen_trace(db, as_of):
    stages = [r["stage"] for r in q(db, "SELECT stage FROM v_stage ORDER BY ord")]
    cells = {(r["pr"], r["stage"]): r for r in q(db, "SELECT * FROM v_trace")}
    rows = []
    for s in q(db, "SELECT * FROM v_trace_summary ORDER BY pr DESC"):
        line = f'<b>#{esc(s["pr"])}</b> {esc(s["title"])}<div class="muted small">{esc(s["author"])} · {esc(s["freight"])}'
        line += f' · release PR #{esc(s["release_pr"])}' if s["release_pr"] else ""
        line += "</div>"
        where = f'in <b>{esc(s["furthest_stage"])}</b> since {esc(fmt_ts(s["furthest_at"]))}' if s["furthest_stage"] else "nowhere yet"
        if s["next"]:
            where += f'<div class="small amber-text">next — {esc(s["next"].split(" (since")[0])}</div>'
        if s["note"]:
            where += f'<div class="small muted">{esc(s["note"])}</div>'
        row = [line, where]
        for st in stages:
            c = cells[(s["pr"], st)]
            if c["reached_at"]:
                row.append(f'<span class="reached">{esc(fmt_ts(c["reached_at"]))}</span>')
            elif c["awaiting"]:
                row.append('<span class="chip amber"><span class="glyph">○</span>awaiting</span>')
            else:
                row.append('<span class="muted">—</span>')
        rows.append(row)
    body = table(["change", "where is it", *stages], rows, "trace")
    return section("trace", "3 · where is my change", "PR → freight → stages",
                   "Keith's tracker reconstructs this by mining CI logs and commit subjects. Here it is a join: the "
                   "freight names its PRs, the lanes say where the freight is. A PR merged after the cut is not "
                   "<em>unreleased</em> — it is in testing, with a timestamp, because testing is a stage.",
                   body, sql_block(db, "v_trace", "v_trace_summary"))


def screen_diffgate(db, as_of):
    rows = []
    for r in q(db, "SELECT g.*, pl.n_create, pl.n_update, pl.n_delete, pl.n_replace, pl.migrations, pl.against "
                   "FROM v_gate_all g LEFT JOIN v_plan pl ON pl.stage=g.stage AND pl.freight=g.freight "
                   "WHERE g.stage='production-eu' ORDER BY g.freight"):
        if r["plan_fact"] is None:
            plan, outcome = '<span class="muted">no plan yet</span>', '<span class="muted">not evaluable — plan missing</span>'
        else:
            plan = (f'+{esc(r["n_create"])} ~{esc(r["n_update"])} −{esc(r["n_delete"])} ±{esc(r["n_replace"])} '
                    f'{fact_ref(r["plan_fact"])}<div class="small muted">against {esc(r["against"])}</div>')
            if r["migrations_changed"]:
                plan += f'<div class="small"><span class="glyph">⚠</span> migrations: {esc(", ".join(json.loads(r["migrations"]) if r["migrations"] else []))}</div>'
            if r["plan_safe"]:
                outcome = '<span class="chip ok"><span class="glyph">▸</span>auto</span><div class="small">safe plan — no approval needed</div>'
            elif r["approved_at"]:
                outcome = (f'<span class="chip ok"><span class="glyph">✓</span>approved</span><div class="small">not safe → required oncall; '
                           f'{esc(r["approved_by"])} at {esc(fmt_ts(r["approved_at"]))} {fact_ref(r["approval_fact"])}</div>')
            else:
                outcome = '<span class="chip amber"><span class="glyph">○</span>would require approval</span><div class="small">not safe — evaluated ahead of candidacy</div>'
        upstream = f'production carried it {esc(fmt_ts(r["upstream_carried_at"]))}' if r["upstream_carried_at"] else '<span class="muted">production does not carry it yet</span>'
        rows.append([mono(r["freight"]), plan, upstream, outcome])
    pol = one(db, "SELECT * FROM v_policy WHERE stage='production-eu'")
    body = (f'<p class="lede">production-eu policy: <code>{esc(pol["rule"])}</code><br>'
            f'where <em>safe</em> = <code>{esc(pol["safe_rule"])}</code></p>'
            + table(["freight", "plan for production-eu", "upstream", "gate outcome"], rows))
    return section("diffgate", "4 · a diff-gate", "\"If migrations changed, require approval; otherwise fast-track\"",
                   "Joe's question, answered as a query: the plan is a fact (stage 3 of the key decomposition — "
                   "desired × world at T), so a gate can read it. Three freights, same rule, three different outcomes. "
                   "The rule is evaluable before the freight is even a candidate — the plan for F418 was computed ahead.",
                   body, sql_block(db, "v_plan"))


def screen_uptake(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_pending_uptake ORDER BY consumer"):
        if r["pending"]:
            state = (f'<span class="chip amber"><span class="glyph">○</span>v{esc(r["published_version"])} available, {esc(r["policy"])}</span>'
                     f'<div class="small">since {esc(fmt_ts(r["published_at"]))} · {esc(fmt_since(r["published_at"], as_of))} · '
                     f'<a href="#" class="preview">{esc(r["preview"])}</a></div>')
        else:
            state = f'<span class="chip ok"><span class="glyph">●</span>current at v{esc(r["consumed_version"])}</span>'
        rows.append([
            f'<b>{esc(r["consumer"])}</b><div class="small muted">{esc(r["key"])} ← {esc(r["producer"])}</div>',
            f'v{esc(r["published_version"])} {mono(r["published_value"])}<div class="small muted">published {esc(fmt_ts(r["published_at"]))} {fact_ref(r["published_fact"])}</div>',
            f'v{esc(r["consumed_version"])}<div class="small muted">{esc(fmt_ts(r["consumed_at"]))} · {esc(r["consumed_by"])}</div>',
            state + f'<div class="small muted">{esc(r["description"])}</div>',
        ])
    body = table(["consumer · binding", "published (evidence)", "taken up (intent)", "state"], rows)
    return section("uptake", "5 · a pending uptake", "Publication is evidence; uptake is intent",
                   "The AMI bake published a new record. The worker pool's binding declares a gated uptake, so the "
                   "grid shows <em>available, gated</em> with a preview — instead of an instance refresh nobody "
                   "decided on. Blast radius is <code>consumers WHERE pending</code>: one.",
                   body, sql_block(db, "v_pending_uptake", "v_record", "v_uptaken"))


def screen_outofband(db, as_of):
    o = one(db, "SELECT * FROM v_observed WHERE stage='production'")
    bg = one(db, "SELECT * FROM v_breakglass WHERE stage='production'")
    d = one(db, "SELECT * FROM v_desired WHERE stage='production'")
    if not o:
        return section("oob", "the side door", "Out-of-band reality", "", '<p class="muted">no conformance reads yet.</p>', sql_block(db, "v_observed"))
    services = json.loads(o["services"])
    rows = []
    for svc, fr in services.items():
        ok = fr == d["freight"]
        rows.append([mono(svc), mono(d["freight"]), mono(fr),
                     '<span class="chip ok"><span class="glyph">●</span>matches</span>' if ok
                     else '<span class="chip bad"><span class="glyph">⚠</span>differs</span>'])
    body = (f'<p class="lede">Latest conformance read of production, {esc(fmt_ts(o["ts"]))} ({esc(o["source"])}) {fact_ref(o["fact"])}.</p>'
            + table(["service", "should run", "runs", ""], rows))
    if bg:
        body += (f'<div class="callout"><span class="cls intent">intent</span> <b>break-glass</b> {fact_ref(bg["fact"])} · '
                 f'{esc(fmt_ts(bg["ts"]))} · {esc(bg["actor"])} · {esc(bg["incident"])}<br>'
                 f'<em>{esc(bg["action"])}</em> on <code>{esc(bg["scope"])}</code>, {esc(bg["from_freight"])} → {esc(bg["to_freight"])}; '
                 f'expires {esc(bg["expiry"])}.<br><span class="muted">{esc(bg["rationale"])}</span></div>'
                 '<p class="small">The drift is <b>explained</b>: the side door wrote the same class of fact, with the same '
                 'attribution, as a front-door promotion. Automation can respect it instead of undoing it, and the '
                 'adopt-vs-revert question is decidable — here the fix ships forward in F418.</p>')
    return section("oob", "the side door", "Out-of-band reality, recorded",
                   "Monday night oncall rolled one ECS service back by hand. The watch saw it; the break-glass fact "
                   "explains it. Drift with no such fact would render as <em>UNEXPLAINED</em>.",
                   body, sql_block(db, "v_observed", "v_breakglass"))


def screen_releases(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_releases"):
        rows.append([
            f'<b>{esc(r["freight"])}</b><div class="small muted">PR #{esc(r["release_pr"])} · {esc(r["release_branch"])} · {esc(r["sha"])} · {esc(r["prs"])} PRs</div>',
            esc(fmt_ts(r["cut_at"])), esc(fmt_ts(r["staging_at"])),
            (f'{esc(r["approved_by"])}<div class="small muted">{esc(fmt_ts(r["approved_at"]))}</div>' if r["approved_at"] else '<span class="chip amber"><span class="glyph">○</span>awaiting</span>'),
            esc(fmt_ts(r["production_at"])) or '<span class="muted">—</span>',
            esc(fmt_ts(r["production_eu_at"])) or '<span class="muted">—</span>',
        ])
    body = table(["release", "cut", "staging", "approved", "production", "production-eu"], rows)
    return section("releases", "Keith parity", "Release trains",
                   "The past-releases cards: cut, staged, approved by whom, live where. One query over lanes and approvals.",
                   body, sql_block(db, "v_releases"))


def screen_transitions(db, as_of):
    out = []
    for t in q(db, "SELECT * FROM v_transition WHERE transition IN ('transition:T417-prod','transition:T418-staging') ORDER BY started_at"):
        facts = q(db, "SELECT * FROM facts WHERE subject=? ORDER BY ts", t["transition"])
        rows = []
        for f in facts:
            p = json.loads(f["payload"])
            what = {"transition.started": f'started · {esc(p.get("strategy") or "")}',
                    "transition.phase": f'phase <b>{esc(p.get("phase"))}</b> — {esc(p.get("detail"))}',
                    "resource.step": f'{esc(p.get("op"))} {mono(p.get("type"))}<div class="small muted">{esc(p.get("urn"))}</div>',
                    "transition.finished": f'finished · <b>{esc(p.get("outcome"))}</b> · update #{esc(p.get("ops_update"))} · {esc(json.dumps(p.get("summary")))}',
                    }.get(f["kind"], esc(f["kind"]))
            rows.append([esc(fmt_ts(f["ts"])), class_chip(f["class"]), mono(f["kind"]), what, fact_ref(f["id"])])
        summary = (f'<h3>{mono(t["transition"])} — {esc(t["freight"])} → {esc(t["stage"])}</h3>'
                   f'<p class="small muted">{esc(fmt_ts(t["started_at"]))} → {esc(fmt_ts(t["finished_at"])) or "in flight"} · '
                   f'{esc(t["resource_steps"])} resource steps recorded · run {esc(t["run"])}</p>')
        out.append(summary + table(["when", "class", "kind", "", "fact"], rows))
    return section("transitions", "fact grain ≠ step grain", "Inside a transition",
                   "A transition is the enactment reified: one subject, many facts across one or more executions. "
                   "Position lives in phase facts the grid can read (Monday's rollout ran both versions live for five "
                   "minutes before cutover); resource steps land on the same subject when the executor emits them.",
                   "".join(out), sql_block(db, "v_transition"))


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


SCREENS = [screen_grid, screen_lanes, screen_awaiting, screen_trace, screen_diffgate,
           screen_uptake, screen_outofband, screen_releases, screen_transitions, screen_ledger]


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
<div class="legend"><span>● converged</span><span>○ awaiting (nothing running)</span><span>◌ in flight</span><span>⊘ hold</span><span>⚠ drift</span><span>✕ failed</span><span>fact ids link into the ledger tail</span></div>
{''.join(snap_html)}
<footer>Fixture: the pulumi-service weekday release train (testing on every master push; staging from the release PR;
oncall's merge is production's approval; production-eu follows when the plan is boring), plus the worker-pool AMI edge.
Hand-written facts; the views are real. Source: <code>~/src/nyobe/delivery-ledger</code>.</footer>
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
