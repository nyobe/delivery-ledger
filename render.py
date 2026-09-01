#!/usr/bin/env python3
"""facts.jsonl → sqlite → views → the tracker, as one HTML page.

    ./render.py                                   lint, self-check, render out/pulumi-service/index.html
    ./render.py --scenario multistack             another scenario directory under scenarios/
    ./render.py --lint-only                       lint + self-checks, no HTML
    ./render.py --as-of 2026-08-24T16:20:00Z      render the ledger as it stood then
    ./render.py --pure                            views evaluated on demand, no per-build materialisation

Everything the page shows is a SELECT over the `facts` table (schema.sql,
views.sql). The renderer holds no state: it loads the facts whose timestamp is
at or before `--as-of`, sets the clock, and formats query results. Rendering
the same ledger at several instants is the cheapest proof that the ledger, not
the UI, is where state lives — the page carries a snapshot switcher for that.
"""
import argparse
import copy
import html
import importlib.util
import json
import os
import re
import sqlite3
import sys
import types
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
FORBIDDEN_VALUES = {"awaiting", "drifted", "converged", "held", "pending", "ready", "in-flight", "superseded", "idle", "partial"}



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
            if not p.get("program") or not p.get("stacks"):
                err(i, f, "warehouse.declared needs program and stacks (the stacks its freight enacts)")
        elif kind == "stage.declared":
            declared["stage"].add(sname)
            if not p.get("program") or not p.get("environment"):
                err(i, f, "stage.declared needs program and environment")
            up = p.get("upstream", "")
            if up.startswith("warehouse:") and up[10:] not in declared["warehouse"]:
                err(i, f, f"upstream {up} is not a declared warehouse")
        elif kind == "freight.discovered":
            declared["freight"].add(sname)
            if p.get("warehouse") not in declared["warehouse"]:
                err(i, f, f"freight.discovered names warehouse {p.get('warehouse')!r}, not a declared warehouse")
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

# The views are the product; whether each one is evaluated on demand or
# materialised once per build is a rendering choice. By default build_db
# materialises every view, in file order (views.sql is layered bottom-up), so
# each layer is computed once from the layer below instead of being re-derived
# inside every correlated subquery above it. `--pure` leaves them as views and
# must render the same page — that is the check that nothing depends on the
# materialisation.
PURE = False
VIEW_SQL = {}  # name -> the CREATE VIEW text, for the page's "the query" panels


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
    views = [(r["name"], r["sql"]) for r in q(db, "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY rowid")]
    VIEW_SQL.update(views)
    if not PURE:
        for name, sql in views:
            body = re.split(r"^CREATE VIEW \S+ AS\s+", sql, maxsplit=1)[1]
            db.execute(f"DROP VIEW {name}")
            db.execute(f"CREATE TABLE {name} AS {body}")
    return db


def q(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(db, sql, *args):
    rows = q(db, sql, *args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Scenarios: a directory with facts.jsonl (from fixture.py) and scenario.py
# (page chrome, snapshot instants, self-checks). The views are shared.
# ---------------------------------------------------------------------------

def load_scenario(name):
    path = os.path.join(HERE, 'scenarios', name, 'scenario.py')
    if not os.path.exists(path):
        raise SystemExit(f'no scenario {name!r} ({path})')
    spec = importlib.util.spec_from_file_location(f'scenario_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def self_checks(scenario, facts):
    checks = []  # (name, as_of, mutation-or-None, predicate)

    def check(name, as_of, fn, mutation=None):
        checks.append((name, as_of, mutation, fn))

    scenario.self_checks(check, types.SimpleNamespace(q=q, one=one, grid=grid, find=find), facts)

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
        if n in VIEW_SQL:
            parts.append(VIEW_SQL[n] + ";")
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
        elif st == "partial":
            detail = f'{esc(r["n_stacks_carrying"])}/{esc(r["n_stacks"])} stacks · {esc(r["stacks_detail"])}'
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
            (f'{mono(r["carried"])}<div class="muted small">since {esc(fmt_ts(r["carried_since"]))} · {esc(fmt_since(r["carried_since"], as_of))} · update #{esc(r["ops_update"])}</div>' if r["carried"]
             else (f'<span class="muted">mixed</span><div class="muted small">{esc(r["stacks_detail"])}</div>' if r["stacks_detail"] else '<span class="muted">nothing yet</span>')),
        ])
    body = table(["stage", "state (derived)", "should carry (intent)", "carries (observed)"], rows, "grid")
    return section("grid", "1 · the subject grid", "What is where, since when, waiting on whom",
                   "One row per stage. <em>Should carry</em> is the latest promotion decision; <em>carries</em> is the "
                   "latest enactment that finished; the state column joins the two with the gate evaluation, active "
                   "holds, in-flight or failed enactments, and the last conformance read. Nothing in it is stored.",
                   body, sql_block(db, "v_grid", "v_gate_eval", "v_candidate"))


def lane_cell(c, as_of, via=None):
    """Format one freight×stage (or PR×stage) cell from its view-computed `cell` class."""
    kind = c["cell"]
    if kind == "reached":
        extra = " " + chip("converged", "current", "ok tiny") if c.get("is_current") else ""
        if via:
            extra += f'<div class="small muted">{esc(via)}</div>'
        return f'<span class="reached">{esc(fmt_ts(c["reached_at"]))}</span>{extra}'
    if kind == "in-flight":
        return chip("in-flight", "in flight") + f'<div class="small muted">since {esc(fmt_ts(c["inflight_since"]))}</div>'
    if kind == "awaiting":
        since = f'<br><span class="muted">{esc(fmt_since(c["awaiting_since"], as_of))}</span>' if c.get("awaiting_since") else ""
        return chip("awaiting") + f'<div class="small">{esc(c["awaiting"])}{since}</div>'
    if kind in ("failed", "superseded"):
        when = f'<div class="small muted">{esc(fmt_ts(c["last_outcome_at"]))}</div>' if c.get("last_outcome_at") else ""
        return chip(kind, c["last_outcome"]) + when
    return '<span class="muted">—</span>'


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
            cells.append(lane_cell(c, as_of))
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
            row.append(lane_cell(c, as_of, via=c["via"] if c["via"] and c["via"] != s["introduced_in"] else None))
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
        for r in q(db, "SELECT g.*, t.term_outcome, t.evidence AS term_evidence, t.evidence_fact AS term_fact, t.unmet_text AS term_unmet, t.role, "
                       "pl.n_create, pl.n_update, pl.n_delete, pl.n_replace, pl.migrations, pl.migrations_changed, pl.against, pl.fact AS plan_fact "
                       "FROM v_gate g JOIN v_gate_term t ON t.stage=g.stage AND t.freight=g.freight AND t.type='plan_safe_or_approved' "
                       "LEFT JOIN v_plan pl ON pl.stage=g.stage AND pl.freight=g.freight WHERE g.stage=? ORDER BY g.freight", st):
            if r["plan_fact"] is None:
                plan = '<span class="muted">no plan yet</span>'
            else:
                plan = (f'+{esc(r["n_create"])} ~{esc(r["n_update"])} −{esc(r["n_delete"])} ±{esc(r["n_replace"])} {fact_ref(r["plan_fact"])}'
                        f'<div class="small muted">against {esc(r["against"])}</div>')
                if r["migrations_changed"]:
                    plan += f'<div class="small"><span class="glyph">⚠</span> migrations: {esc(", ".join(json.loads(r["migrations"]) if r["migrations"] else []))}</div>'
            term = {
                "auto":     chip("ready", "auto") + '<div class="small">safe plan — no approval needed</div>',
                "approved": chip("converged", "approved") + f'<div class="small">not safe → {esc(r["role"])} approved: {esc(r["term_evidence"])} {fact_ref(r["term_fact"])}</div>',
                "no-plan":  chip("awaiting", "no plan") + f'<div class="small">{esc(r["term_unmet"])}</div>',
                "open":     chip("awaiting", "open") + f'<div class="small">{esc(r["term_unmet"])}</div>',
            }[r["term_outcome"]]
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
    n_pending = one(db, "SELECT count(*) AS n FROM v_pending_uptake WHERE pending = 1")["n"]
    return section("uptake", "5 · uptake, gated and auto", f'Publication is evidence; uptake is intent — {n_pending} pending',
                   "Each binding declares its uptake policy. On a gated edge a new record renders as <em>available, "
                   "gated</em> with a preview until someone decides; on an auto edge the policy writes the uptake "
                   "decision the moment the producer publishes. Blast radius is <code>consumers WHERE pending</code>.",
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
    stages = [r["stage"] for r in q(db, "SELECT stage FROM v_stage ORDER BY ord")]
    cells = {(r["freight"], r["stage"]): r for r in q(db, "SELECT * FROM v_release_stage")}
    rows = []
    for r in q(db, "SELECT * FROM v_releases"):
        row = [f'<b>{esc(r["freight"])}</b><div class="small muted">PR #{esc(r["release_pr"])} · {esc(r["release_branch"])} · {esc(r["sha"])}</div>'
               f'<div class="small muted">{esc(r["prs"])} PRs: {esc(r["pr_list"])}</div>',
               esc(fmt_ts(r["cut_at"]))]
        for st in stages:
            c = cells[(r["freight"], st)]
            cell = lane_cell(c, as_of)
            if c["approvable"]:
                cell = (f'<div class="small">approved by {esc(c["approved_by"])} · {esc(fmt_ts(c["approved_at"]))}</div>' if c["approved_at"]
                        else '<div class="small muted">no approval</div>') + cell
            row.append(cell)
        rows.append(row)
    body = table(["release", "cut", *stages], rows)
    return section("releases", "Keith parity", "Release trains",
                   "The past-releases cards: cut, then per stage the approval on record (where the stage's policy has "
                   "an approval term) and when the release landed there; shipping which PRs (those introduced since "
                   "the previous cut). One query over lanes, approvals and membership.",
                   body, sql_block(db, "v_releases", "v_release_stage", "v_release_prs"))


def screen_transitions(db, as_of):
    out = []
    ts_ = q(db, "SELECT * FROM v_freight_transition WHERE last_phase IS NOT NULL OR resource_steps > 0 OR outcome IN ('failed','abandoned') ORDER BY started_at")
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
        if d["n_required"] == 0:
            approvals = '<span class="muted">none required</span>'
        else:
            approvals = f'{esc(d["n_required"] - d["n_unmet"])}/{esc(d["n_required"])} met ' + fact_ref(d["evidence"])
            if d["unmet"]:
                approvals += f'<div class="small bad-text">unmet: {esc(d["unmet"])}</div>'
        rows.append([esc(fmt_ts(d["ts"])), f'<b>{esc(d["stage"])}</b> ← {mono(d["freight"])}', esc(d["actor"]),
                     approvals, esc(d["n_refs"]),
                     (f'<span class="chip bad"><span class="glyph">⚠</span>{esc(d["flag"])}</span>' if d["flag"] else chip("converged", "clean")),
                     fact_ref(d["fact"])])
    n_flag = one(db, "SELECT count(*) AS n FROM v_audit_flag WHERE flag IS NOT NULL")["n"]
    return section("audit", "the ledger records what it is told", f'Decision audit — {n_flag} flagged',
                   "Every promotion decision, checked against the policy that should have written it: was it written "
                   "by the stage's policy; was every approval-bearing term (an approval, or a safe plan) on record "
                   "when it was written; did it cite evidence? An unauthorised or unevidenced decision is still a "
                   "fact — this is how it stays distinguishable from a legitimate one.",
                   table(["when", "decision", "written by", "approval terms at decision time", "refs", "audit", "fact"], rows),
                   sql_block(db, "v_audit_term", "v_audit_decision", "v_audit_flag"))


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


def render_page(facts, snapshots, page):
    snap_html, buttons = [], []
    for i, (as_of, caption) in enumerate(snapshots):
        db = build_db(facts, as_of)
        body = "".join(fn(db, as_of) for fn in SCREENS)
        snap_html.append(f'<div class="snapshot" data-asof="{esc(as_of)}"{"" if i == 0 else " hidden"}>{body}</div>')
        buttons.append(f'<button type="button" data-asof="{esc(as_of)}" aria-pressed="{"true" if i == 0 else "false"}">'
                       f'{esc(caption)}<span class="t">{esc(as_of)}</span></button>')
    return f"""<title>{esc(page["title"])}</title>
<meta name="description" content="{esc(page["description"])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:ital,wght@0,400;0,600;1,400&family=Source+Serif+4:opsz,wght@8..60,600&display=swap">
<style>{CSS}</style>
<div class="page">
<header class="masthead">
  <div>
    <div class="kicker">{esc(page["kicker"])}</div>
    <h1>{esc(page["title"])}</h1>
    <p class="thesis">{page["thesis"]}</p>
  </div>
  <div class="clock"><div class="label">ledger as of</div><div class="now">{esc(snapshots[0][0])}</div></div>
</header>
<nav class="snaps" aria-label="ledger snapshots">{''.join(buttons)}</nav>
<div class="legend"><span>● converged</span><span>○ awaiting (nothing running)</span><span>◌ in flight</span><span>↷ superseded</span><span>⊘ hold</span><span>⚠ drift</span><span>✕ failed</span><span>fact ids link into the ledger tail</span></div>
{''.join(snap_html)}
<footer>{page["footer"]} Source: <code>~/src/nyobe/delivery-ledger</code>.</footer>
</div>
<script>{JS}</script>
"""


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="pulumi-service", help="directory under scenarios/ (default: pulumi-service)")
    ap.add_argument("--facts", help="facts file (default: scenarios/<scenario>/facts.jsonl)")
    ap.add_argument("--out", help="output page (default: out/<scenario>/index.html)")
    ap.add_argument("--as-of", action="append", help="render the ledger as of this instant (repeatable; first is primary)")
    ap.add_argument("--lint-only", action="store_true")
    ap.add_argument("--pure", action="store_true", help="evaluate every view on demand instead of materialising per build (slow; must give the same page)")
    args = ap.parse_args()
    global PURE
    PURE = args.pure

    scenario = load_scenario(args.scenario)
    facts = load_facts(args.facts or os.path.join(HERE, "scenarios", args.scenario, "facts.jsonl"))
    out = args.out or os.path.join(HERE, "out", args.scenario, "index.html")
    errors = lint(facts)
    if errors:
        print(f"lint: {len(errors)} problem(s)", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1
    print(f"lint: {len(facts)} facts, clean")

    n, failures = self_checks(scenario, facts)
    if failures:
        print(f"self-check: {len(failures)}/{n} failed", file=sys.stderr)
        for f in failures:
            print("  ✗ " + f, file=sys.stderr)
        return 1
    print(f"self-check: {n}/{n} passed")
    if args.lint_only:
        return 0

    snapshots = [(a, a) for a in args.as_of] if args.as_of else scenario.SNAPSHOTS
    page = render_page(facts, snapshots, scenario.PAGE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(page)
    print(f"rendered {out} ({len(page)//1024} KB, {len(snapshots)} snapshot(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
