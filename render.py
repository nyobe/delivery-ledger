#!/usr/bin/env python3
"""facts.jsonl → sqlite → views → the tracker, as one HTML page.

    ./render.py                                   lint, self-check, render out/pulumi-service/index.html
    ./render.py --scenario multistack             another scenario directory under scenarios/
    ./render.py --lint-only                       lint + self-checks, no HTML
    ./render.py --as-of 2026-08-24T16:20:00Z      render the ledger as it stood then
    ./render.py --pure                            views evaluated on demand, no per-build materialisation
    ./render.py --pure-check                      every view's rows, materialised vs pure, at the primary instant

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
    "release.pinned", "promotion.decided", "approval.granted", "hold.placed", "breakglass.recorded",
    "uptake.decided",
}
OBSERVATION_KINDS = {
    "freight.discovered", "transition.started", "transition.phase", "transition.finished",
    "resource.step", "verification.recorded", "job.finished", "plan.summarized",
    "output.published", "state.observed",
}
RATIONALE_REQUIRED = {
    "release.cut", "release.pinned", "promotion.decided", "approval.granted", "hold.placed",
    "breakglass.recorded", "uptake.decided",
}
SUBJECT_TYPES = {"warehouse", "stage", "freight", "transition", "edge", "record", "release"}
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
        if kind == "release.pinned":
            for prog, fr in (p.get("members") or {}).items():
                if fr not in declared["freight"]:
                    err(i, f, f"pin-set member {prog}: {fr} is not a discovered freight")

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


def build_db(facts, as_of, pure=None):
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
    if not (PURE if pure is None else pure):
        for name, sql in views:
            body = re.split(r"^CREATE VIEW \S+ AS\s+", sql, maxsplit=1)[1]
            db.execute(f"DROP VIEW {name}")
            db.execute(f"CREATE TABLE {name} AS {body}")
    return db


def q(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def pure_check(facts, as_of, step_budget=200_000_000):
    """Row-for-row identity of every view between a materialised build and a pure one.

    A view pure mode cannot evaluate — SQLite's limit on table references once the
    views are inlined, or a step budget blown — is reported as skipped, not as a
    difference: the point is that every view it *can* evaluate agrees.
    """
    mat = build_db(facts, as_of, pure=False)
    pur = build_db(facts, as_of, pure=True)
    steps = [0]
    def tick():
        steps[0] += 1
        return steps[0] > step_budget // 1000
    pur.set_progress_handler(tick, 1000)
    same, differ, skipped = [], [], []
    for name in VIEW_SQL:
        rows_m = sorted(json.dumps(r, sort_keys=True, default=str) for r in q(mat, f"SELECT * FROM {name}"))
        steps[0] = 0
        try:
            rows_p = sorted(json.dumps(r, sort_keys=True, default=str) for r in q(pur, f"SELECT * FROM {name}"))
        except sqlite3.OperationalError as e:
            skipped.append((name, "interrupted: step budget" if "interrupted" in str(e) else str(e)))
            continue
        (same if rows_m == rows_p else differ).append(name)
    print(f"pure-check at {as_of}: {len(same)} views identical, {len(differ)} differ, {len(skipped)} pure mode could not evaluate")
    for n in differ:
        print(f"  ✗ {n} differs")
    for n, why in skipped:
        print(f"  – {n}: {why}")
    return 1 if differ else 0


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
    "partial": ("◐", "amber"), "complete": ("●", "ok"),
    "current": ("●", "ok"), "behind": ("○", "amber"), "unpinned": ("○", "hold"), "nothing": ("·", "hold"),
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

def programs(db):
    """Programs in stage order, each with its stages — every per-program screen pivots over this."""
    out = []
    for r in q(db, "SELECT program, min(ord) AS o FROM v_stage GROUP BY program ORDER BY o"):
        out.append((r["program"], [x["stage"] for x in q(db, "SELECT stage FROM v_stage WHERE program=? ORDER BY ord", r["program"])]))
    return out


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
            if r["n_inflight"] and r["n_inflight"] > 1:
                detail += f'<div class="muted small">{esc(r["n_inflight"])} legs in flight: {esc(r["inflight_detail"])}</div>'
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
    if kind == "partial":
        return chip("partial") + f'<div class="small muted">{esc(c.get("partial_detail") or "")}</div>'
    if kind == "awaiting":
        since = f'<br><span class="muted">{esc(fmt_since(c["awaiting_since"], as_of))}</span>' if c.get("awaiting_since") else ""
        return chip("awaiting") + f'<div class="small">{esc(c["awaiting"])}{since}</div>'
    if kind in ("failed", "superseded"):
        when = f'<div class="small muted">{esc(fmt_ts(c["last_outcome_at"]))}</div>' if c.get("last_outcome_at") else ""
        return chip(kind, c["last_outcome"]) + when
    return '<span class="muted">—</span>'


def screen_lanes(db, as_of):
    lanes = {(r["freight"], r["stage"]): r for r in q(db, "SELECT * FROM v_lanes")}
    progs = programs(db)
    parts = []
    for prog, stages in progs:
        freights = q(db, "SELECT DISTINCT fr.freight, fr.release_pr, fr.cut_at, fr.discovered_at, fr.branch FROM v_freight fr "
                         "JOIN v_warehouse w ON w.name = fr.warehouse WHERE w.program = ? ORDER BY fr.discovered_at DESC", prog)
        rows = []
        for fr in freights:
            label = f'<b>{esc(fr["freight"])}</b>'
            label += (f'<div class="muted small">release PR #{esc(fr["release_pr"])} · cut {esc(fmt_ts(fr["cut_at"]))}</div>' if fr["release_pr"]
                      else f'<div class="muted small">{esc(fr["branch"])} build · {esc(fmt_ts(fr["discovered_at"]))}' + (" · no release yet" if len(progs) == 1 else "") + '</div>')
            cells = [label]
            for st in stages:
                cells.append(lane_cell(lanes[(fr["freight"], st)], as_of))
            rows.append(cells)
        head = f'<h3>{esc(prog)}</h3>' if len(progs) > 1 else ""
        parts.append(head + table(["freight"] + stages, rows, "lanes"))
    blurb = ("Each freight runs through the stages it has reached; a cell is the time the enactment finished "
             "there, or what the freight is waiting on, or how its last attempt ended. Under content keying the "
             "build that ran in testing would <em>be</em> the release — today pulumi-service rebuilds per stage "
             "from the git SHA, so this lane is the thesis's construct, not yet the pipeline's." if len(progs) == 1 else
             "Each program's freight runs through that program's stages; a cell is the time the enactment finished "
             "there on every one of the stage's stacks, <em>partial</em> when only some have, or what the freight "
             "is waiting on, or how its last attempt ended.")
    return section("lanes", "1 · freight lanes", "Freight × stage", blurb, "".join(parts), sql_block(db, "v_lanes"))


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
    cells = {(r["warehouse"], r["pr"], r["stage"]): r for r in q(db, "SELECT * FROM v_trace_cell")}
    progs = programs(db)
    parts = []
    for prog, stages in progs:
        rows = []
        for s_ in q(db, "SELECT t.* FROM v_trace_summary t JOIN v_warehouse w ON w.name = t.warehouse WHERE w.program = ? ORDER BY t.pr DESC", prog):
            line = f'<b>#{esc(s_["pr"])}</b> {esc(s_["title"])}<div class="muted small">{esc(s_["author"])} · merged in {esc(s_["introduced_in"])}'
            line += f' · shipped in release PR #{esc(s_["shipped_in"])}' if s_["shipped_in"] else ""
            line += "</div>"
            if s_["furthest_stage"]:
                where = f'in <b>{esc(s_["furthest_stage"])}</b> since {esc(fmt_ts(s_["furthest_at"]))}'
                if s_["furthest_via"] != s_["introduced_in"]:
                    where += f' <span class="muted">(via {esc(s_["furthest_via"])})</span>'
            else:
                where = "nowhere yet"
            if s_["next"]:
                where += f'<div class="small amber-text">next — {esc(s_["next"])}</div>'
            if s_["note"]:
                where += f'<div class="small muted">{esc(s_["note"])}</div>'
            row = [line, where]
            for st in stages:
                c = cells[(s_["warehouse"], s_["pr"], st)]
                row.append(lane_cell(c, as_of, via=c["via"] if c["via"] and c["via"] != s_["introduced_in"] else None))
            rows.append(row)
        head = f'<h3>{esc(prog)}</h3>' if len(progs) > 1 else ""
        parts.append(head + table(["change", "where is it", *stages], rows, "trace"))
    return section("trace", "3 · where is my change", "PR → freight → stages",
                   "Keith's tracker reconstructs this by mining CI logs and commit subjects. Here it is a join: a "
                   "freight names the PRs it introduced, membership is cumulative along its warehouse's branch, the "
                   "lanes say where each freight is. A PR merged after the cut is not <em>unreleased</em> — it is in "
                   "the first stage, with a timestamp, because that is a stage.",
                   "".join(parts), sql_block(db, "v_membership", "v_trace_cell", "v_trace_summary"))


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


def plan_counts(r, prefix="p_"):
    return f'+{esc(r[prefix + "create"])} ~{esc(r[prefix + "update"])} −{esc(r[prefix + "delete"])} ±{esc(r[prefix + "replace"])}'


def screen_uptake(db, as_of):
    rows = []
    for r in q(db, "SELECT * FROM v_pending_uptake ORDER BY consumer_program, environment, consumer, key"):
        # what the consumer is wired with now, and what has been published
        if r["pending"] is None:
            state = '<span class="muted">nothing published yet</span>'
        elif r["pending"]:
            if r["n_terms"] is not None:
                if r["passes"]:
                    state = chip("ready", f'v{r["published_version"]} available — gate passes') + '<div class="small">the policy has not written the uptake yet</div>'
                else:
                    state = (chip("awaiting", f'v{r["published_version"]} available, {r["policy"]}')
                             + f'<div class="small">awaiting <b>{esc(r["awaiting"])}</b> · {esc(fmt_since(r["gate_since"], as_of))}</div>')
            else:
                state = (chip("awaiting", f'v{r["published_version"]} available, {r["policy"]}')
                         + f'<div class="small">since {esc(fmt_ts(r["published_at"]))} · {esc(fmt_since(r["published_at"], as_of))} · a person decides</div>')
        else:
            who = "by policy" if str(r["consumed_by"]).startswith("policy:") else f'by {r["consumed_by"]}'
            state = chip("converged", f'current at v{r["consumed_version"]}') + f'<div class="small muted">taken up {who} · {esc(fmt_ts(r["consumed_at"]))} {fact_ref(r["consumed_fact"])}</div>'
        # the hyper-preview: the consumer's plan against the proposed record
        if r["preview_fact"]:
            preview = (f'{plan_counts(r)} {fact_ref(r["preview_fact"])}'
                       + (chip("ready", "safe", "ok tiny") if r["preview_safe"] else chip("awaiting", "not safe", "amber tiny"))
                       + f'<div class="small muted">{esc(r["preview_note"] or "")}</div>')
        elif r["pending"]:
            preview = f'<span class="muted">no preview computed for v{esc(r["published_version"])}</span>'
        else:
            preview = '<span class="muted">—</span>'
        terms = ""
        if r["n_terms"] is not None and r["pending"]:
            trows = []
            for t in q(db, "SELECT * FROM v_uptake_term WHERE edge=? AND version=? ORDER BY idx", r["edge"], r["published_version"]):
                if t["satisfied_at"] is not None:
                    trows.append(f'{chip("converged", "met", "ok tiny")} {esc(t["label"])} — {esc(t["evidence"] or "")} {fact_ref(t["evidence_fact"])}')
                else:
                    trows.append(f'{chip("awaiting", "open", "amber tiny")} {esc(t["label"])} — <span class="muted">{esc(t["unmet_text"])}</span>')
            terms = '<div class="small">' + "<br>".join(trows) + "</div>"
        pol = f'uptake <b>{esc(r["policy"])}</b>' + (f' · <code>{esc(r["rule"])}</code>' if r["rule"] and r["rule"] != "true" else "")
        rows.append([
            f'<b>{esc(r["consumer"])}</b><div class="small muted">{esc(r["key"])} ← {esc(r["producer"])}</div><div class="small muted">{pol}</div>'
            + (f'<div class="small muted">{esc(r["description"])}</div>' if r["description"] else ""),
            (f'v{esc(r["published_version"])} {mono(r["published_value"])}<div class="small muted">published {esc(fmt_ts(r["published_at"]))} {fact_ref(r["published_fact"])}</div>' if r["published_version"] is not None else '<span class="muted">—</span>'),
            preview,
            (f'v{esc(r["consumed_version"])}<div class="small muted">{esc(fmt_ts(r["consumed_at"]))} · {esc(r["consumed_by"])}</div>' if r["consumed_version"] is not None else '<span class="muted">never</span>'),
            state + terms,
        ])
    body = table(["consumer · binding", "published (evidence)", "preview against it", "taken up (intent)", "state"], rows)
    n_pending = one(db, "SELECT count(*) AS n FROM v_pending_uptake WHERE pending = 1")["n"]

    # by-version pins: published → pinned by a PR → riding the consumer's freight
    pins = q(db, "SELECT * FROM v_pin_uptake ORDER BY consumer_program, key")
    pin_html = ""
    if pins:
        prows = []
        stages_by_prog = dict(programs(db))
        for pn in pins:
            stages = stages_by_prog.get(pn["consumer_program"], [])
            where = {r["stage"]: r for r in q(db, "SELECT * FROM v_pin_stage WHERE edge=? ORDER BY ord", pn["edge"])}
            if pn["published_version"] is None:
                pinned = '<span class="muted">nothing published yet</span>'
            elif pn["pending"]:
                pinned = chip("awaiting", f'v{pn["published_version"]} published, not pinned') + f'<div class="small muted">since {esc(fmt_ts(pn["published_at"]))} · {esc(fmt_since(pn["published_at"], as_of))}</div>'
            else:
                pinned = (chip("converged", f'pinned v{pn["pinned_version"]}')
                          + f'<div class="small muted">{esc(pn["pinned_by"])} · {esc(pn["pinned_via"] or "")} · {esc(fmt_ts(pn["pinned_at"]))} {fact_ref(pn["pinned_fact"])}</div>'
                          + (f'<div class="small muted">in freight {mono(pn["pinned_in"])} · {esc(fmt_ts(pn["pinned_in_at"]))}</div>' if pn["pinned_in"] else '<div class="small muted">no freight carries the pin yet</div>'))
            row = [f'<b>{esc(pn["consumer"])}</b><div class="small muted">{esc(pn["key"])} ← {esc(pn["producer"])} · by-version · uptake <b>{esc(pn["policy"])}</b></div>'
                   + (f'<div class="small muted">{esc(pn["description"])}</div>' if pn["description"] else ""),
                   (f'v{esc(pn["published_version"])} {mono(pn["published_value"])}<div class="small muted">published {esc(fmt_ts(pn["published_at"]))} {fact_ref(pn["published_fact"])}</div>'
                    + (f'<div class="small muted">{esc(pn["published_note"])}</div>' if pn["published_note"] else "")) if pn["published_version"] is not None else '<span class="muted">—</span>',
                   pinned]
            for st in stages:
                w = where.get(st)
                if not w:
                    row.append('<span class="muted">—</span>')
                    continue
                cell = chip(w["pin_state"], f'pin v{w["carried_pin"]}' if w["carried_pin"] is not None else w["pin_state"], None if w["pin_state"] != "behind" else "amber")
                if w["pin_state"] == "behind" and w["cell"] and w["cell"] != "none":
                    cell += f'<div class="small">{mono(w["pinned_in"])} · ' + lane_cell(w, as_of) + "</div>"
                elif w["carried"]:
                    cell += f'<div class="small muted">carries {esc(w["carried"])}</div>'
                row.append(cell)
            prows.append(row)
        pin_html = ('<h3>By version: the pin rides the consumer\'s train</h3>'
                    '<p class="lede">The producer publishes a stage-invariant record; the consumer pins a version in its config. '
                    'The uptake is that config change, however it is made, and from then on it is ordinary freight, meeting '
                    'every stage\'s gates on the way. "Pending" means published but not pinned; where the pin has got to is '
                    'a lanes question, answered per consumer stage on the right.</p>'
                    + table(["consumer · binding", "published (evidence)", "pinned (intent)", *stages], prows))
    return section("uptake", "5 · uptake, gated and auto", f'Publication is evidence; uptake is intent — {n_pending} pending',
                   "Each binding declares its uptake policy, and a by-reference edge's gate is the same kind of thing as "
                   "a stage's: typed terms evaluated at rest against the latest published record. A new record renders "
                   "as <em>available</em> with the consumer's preview against it — the plan is a fact, so the gate can "
                   "read it before anyone decides — until the policy (or a person) writes the uptake. Blast radius is "
                   "<code>consumers WHERE pending</code>.",
                   body + pin_html, sql_block(db, "v_pending_uptake", "v_uptake_term", "v_record_plan", "v_pin_uptake", "v_pin_stage"))


def screen_estate(db, as_of):
    progs = programs(db)
    if len(progs) < 2:
        return ""
    envs = [r["environment"] for r in q(db, "SELECT environment, min(ord) AS o FROM v_stage GROUP BY environment ORDER BY o")]
    cells = {(r["program"], r["environment"]): r for r in q(db, "SELECT * FROM v_estate")}
    rows = []
    for prog, _ in progs:
        row = [f'<b>{esc(prog)}</b><div class="small muted">{esc(one(db, "SELECT owner FROM v_stage WHERE program=? LIMIT 1", prog)["owner"])}</div>']
        for env in envs:
            r = cells.get((prog, env))
            if not r:
                row.append('<span class="muted">—</span>')
                continue
            st = r["status"]
            body = chip(st)
            if r["carried"]:
                body += f' {mono(r["carried"])}'
            elif r["stacks_detail"]:
                body += f'<div class="small muted">{esc(r["stacks_detail"])}</div>'
            if st in ("awaiting", "held"):
                body += f'<div class="small">{esc(r["awaiting"])} · {esc(fmt_since(r["awaiting_since"], as_of))}</div>'
            elif st == "in-flight":
                body += f'<div class="small">{mono(r["inflight_freight"])}' + (f' · {esc(r["last_phase"])}' if r["last_phase"] else "") + "</div>"
            elif st == "failed":
                body += f'<div class="small bad-text">{esc((r["last_error"] or "")[:80])}</div>'
            if r["wired"]:
                body += f'<div class="small muted">wired: {esc(r["wired"])}</div>'
            if r["pending_uptakes"]:
                body += f'<div class="small amber-text">{esc(r["pending_uptakes"])} uptake pending here</div>'
            if r["pending_downstream"]:
                body += f'<div class="small amber-text">{esc(r["pending_downstream"])} downstream uptake pending</div>'
            row.append(body)
        rows.append(row)
    return section("estate", "the org graph is a query", "Programs × environments",
                   "No program owns this grid. Each team declared its own stages and the edges into them; the "
                   "estate view is assembled by joining subjects across programs on their <code>environment</code>. "
                   "<em>Wired</em> is the version vector of records each stage has taken up — what it is actually "
                   "running against, as data.",
                   table(["program", *envs], rows), sql_block(db, "v_estate"))


def screen_pinset(db, as_of):
    pinsets = q(db, "SELECT * FROM v_pinset ORDER BY pinned_at DESC")
    if not pinsets:
        return ""
    envs = [r["environment"] for r in q(db, "SELECT environment, min(ord) AS o FROM v_stage GROUP BY environment ORDER BY o")]
    parts, heads = [], []
    for ps in pinsets:
        members = q(db, "SELECT * FROM v_pinset_member WHERE release=? ORDER BY program", ps["release"])
        order = json.loads(ps["ord"]) if ps["ord"] else [m["program"] for m in members]
        status = {r["environment"]: r for r in q(db, "SELECT * FROM v_pinset_status WHERE release=?", ps["release"])}
        env_rows = {(r["program"], r["environment"]): r for r in q(db, "SELECT * FROM v_pinset_env WHERE release=?", ps["release"])}
        rows = []
        for prog in order:
            m = next(x for x in members if x["program"] == prog)
            row = [f'<b>{esc(prog)}</b> {mono(m["freight"])}']
            for env in envs:
                r = env_rows.get((prog, env))
                if not r:
                    row.append('<span class="muted">—</span>')
                    continue
                cell = lane_cell(r, as_of)
                if r["cell"] == "reached" and not r["carried_now"]:
                    cell += '<div class="small muted">no longer carried</div>'
                row.append(cell)
            rows.append(row)
        srow = ['<b>pin-set</b>']
        for env in envs:
            r = status.get(env)
            if not r:
                srow.append('<span class="muted">—</span>')
                continue
            c = chip(r["state"], f'{r["state"]} {r["members_carried"]}/{r["members"]}')
            if r["complete_at"]:
                c += f'<div class="small muted">since {esc(fmt_ts(r["complete_at"]))}</div>'
            srow.append(c)
        rows.append(srow)
        done = [e for e in envs if status.get(e) and status[e]["state"] == "complete"]
        heads.append(f'{ps["display"] or ps["release"]}: complete in {", ".join(done) if done else "no environment yet"}')
        parts.append(f'<h3>{esc(ps["display"] or ps["release"])} <span class="muted">· pinned {esc(fmt_ts(ps["pinned_at"]))} by {esc(ps["pinned_by"])} {fact_ref(ps["fact"])}</span></h3>'
                     f'<p class="lede">members in order: {" → ".join(esc(o) for o in order)}<br><span class="muted">{esc(ps["rationale"] or "")}</span></p>'
                     + table(["member", *envs], rows))
    return section("pinset", "composite freight", " · ".join(esc(h) for h in heads),
                   "A pin-set is one intent fact naming a member freight per program — a proposal that these ship "
                   "together. It has no enactment of its own: each member moves under its owning team's policy, and "
                   "the pin-set's state per environment is a join over the members' lanes. Values are never pinned "
                   "across stages — each environment enacts the same pair against its own world.",
                   "".join(parts), sql_block(db, "v_pinset", "v_pinset_env", "v_pinset_status"))


def screen_impact(db, as_of):
    roots = q(db, "SELECT DISTINCT root FROM v_impact ORDER BY root")
    if not roots:
        return ""
    parts = []
    for rt in roots:
        rows = []
        for r in q(db, "SELECT * FROM v_impact WHERE root=? ORDER BY depth, consumer", rt["root"]):
            if r["pending"] is None:
                state = '<span class="muted">nothing published</span>'
            elif r["pending"]:
                if r["kind"] == "by-version":
                    state = chip("awaiting", f'v{r["published_version"]} not pinned')
                elif r["passes"] is None:
                    state = chip("awaiting", f'v{r["published_version"]} pending, {r["policy"]}')
                elif r["passes"]:
                    state = chip("ready", f'v{r["published_version"]} gate passes')
                else:
                    state = chip("awaiting", f'v{r["published_version"]} pending') + f'<div class="small">{esc(r["awaiting"])}</div>'
            else:
                state = chip("converged", f'current at v{r["consumed_version"]}') + (f'<div class="small muted">in {mono(r["pinned_in"])}</div>' if r["pinned_in"] else "")
            rows.append([esc(r["depth"]), f'<b>{esc(r["consumer"])}</b><div class="small muted">{esc(r["path"])}</div>',
                         f'{esc(r["key"])} · {esc(r["kind"])} · uptake <b>{esc(r["policy"])}</b>', state])
        parts.append(f'<h3>if <code>{esc(rt["root"])}</code> publishes</h3>' + table(["hop", "downstream", "edge", "right now"], rows))
    return section("impact", "impact analysis is a queue", "What is downstream, and what is waiting",
                   "Tyler's question — \"if I change my stack, how does it affect downstream?\" — is the reverse index over "
                   "binding facts, transitively. Because wiring is data, the answer is a query; because uptake is intent, "
                   "each hop shows whether the last publication has been taken up, waits on a gate, or waits on a pin.",
                   "".join(parts), sql_block(db, "v_impact"))


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
    releases = q(db, "SELECT * FROM v_releases")
    if not releases:
        return ""
    cells = {(r["freight"], r["stage"]): r for r in q(db, "SELECT * FROM v_release_stage")}
    progs = programs(db)
    parts = []
    for prog, stages in progs:
        rows = []
        for r in q(db, "SELECT r.* FROM v_releases r JOIN v_freight fr ON fr.freight = r.freight JOIN v_warehouse w ON w.name = fr.warehouse "
                       "WHERE w.program = ? ORDER BY r.cut_at DESC", prog):
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
        if rows:
            head = f'<h3>{esc(prog)}</h3>' if len(progs) > 1 else ""
            parts.append(head + table(["release", "cut", *stages], rows))
    return section("releases", "Keith parity", "Release trains",
                   "The past-releases cards: cut, then per stage the approval on record (where the stage's policy has "
                   "an approval term) and when the release landed there; shipping which PRs (those introduced since "
                   "the previous cut). One query over lanes, approvals and membership.",
                   "".join(parts), sql_block(db, "v_releases", "v_release_stage", "v_release_prs"))


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
    urows = []
    for d in q(db, "SELECT * FROM v_uptake_audit_flag ORDER BY (flag IS NULL), ts DESC"):
        if d["n_required"] == 0:
            approvals = '<span class="muted">none required</span>'
        else:
            approvals = f'{esc(d["n_required"] - d["n_unmet"])}/{esc(d["n_required"])} met ' + fact_ref(d["evidence"])
            if d["unmet"]:
                approvals += f'<div class="small bad-text">unmet: {esc(d["unmet"])}</div>'
        urows.append([esc(fmt_ts(d["ts"])), f'<b>{esc(d["consumer"])}</b> ← v{esc(d["version"])}<div class="small muted">uptake {esc(d["mode"])}</div>', esc(d["actor"]),
                      approvals, esc(d["n_refs"]),
                      (f'<span class="chip bad"><span class="glyph">⚠</span>{esc(d["flag"])}</span>' if d["flag"] else chip("converged", "clean")),
                      fact_ref(d["fact"])])
    uptake_html = ""
    if urows:
        uptake_html = ('<h3>Uptake decisions</h3><p class="lede">The same check on every uptake written on an edge: by the policy, with the '
                       'edge\'s approval-bearing terms met on record at that instant, citing evidence.</p>'
                       + table(["when", "uptake", "written by", "approval terms at decision time", "refs", "audit", "fact"], urows))
    n_flag = (one(db, "SELECT count(*) AS n FROM v_audit_flag WHERE flag IS NOT NULL")["n"]
              + one(db, "SELECT count(*) AS n FROM v_uptake_audit_flag WHERE flag IS NOT NULL")["n"])
    return section("audit", "the ledger records what it is told", f'Decision audit — {n_flag} flagged',
                   "Every promotion decision, checked against the policy that should have written it: was it written "
                   "by the stage's policy; was every approval-bearing term (an approval, or a safe plan) on record "
                   "when it was written; did it cite evidence? An unauthorised or unevidenced decision is still a "
                   "fact — this is how it stays distinguishable from a legitimate one.",
                   table(["when", "decision", "written by", "approval terms at decision time", "refs", "audit", "fact"], rows) + uptake_html,
                   sql_block(db, "v_audit_term", "v_audit_decision", "v_audit_flag", "v_uptake_audit_term", "v_uptake_audit_flag"))


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


SCREENS = [screen_grid, screen_estate, screen_lanes, screen_gates, screen_trace, screen_diffgate, screen_uptake,
           screen_pinset, screen_impact, screen_outofband, screen_releases, screen_transitions, screen_audit, screen_ledger]


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
    ap.add_argument("--pure", action="store_true", help="evaluate every view on demand instead of materialising per build (slow; the deep views may exceed SQLite's reference limit)")
    ap.add_argument("--pure-check", action="store_true", help="compare every view's rows between a materialised build and a pure one at the primary instant")
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

    if args.pure_check:
        return pure_check(facts, scenario.DEFAULT_AS_OF)

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
