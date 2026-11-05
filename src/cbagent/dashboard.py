"""Static HTML dashboard for the paper rotation books and the GitHub Actions runs that drive them. Read-only.

`collect()` turns data/ (book state, the trade journal, and optionally a GitHub Actions runs JSON) into one dict;
`render()` embeds that dict in a self-contained page (no external scripts). The hourly workflow builds it and
publishes it to GitHub Pages; `cbagent dashboard` builds the same page locally.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .rotation_paper import BOOKS

HOUR = 3600


def _ts(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _journal(data_dir: Path) -> list[dict]:
    path = data_dir / "journal_rotation.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row["t"] = _ts(row.get("ts"))
        rows.append(row)
    return rows


def _book(name: str, state: dict, journal: list[dict], fallback_prices: Callable[[list[str]], dict] | None) -> dict:
    book = BOOKS.get(name)
    start = book.starting_cash if book else 1000.0
    lots: dict[str, list[float]] = {}  # product -> [qty, cost incl. fees]
    realized = 0.0
    last_fill: dict[str, float] = {}
    for row in journal:
        if row.get("book") != name or row.get("event") != "fill":
            continue
        p, q, px, fee = row["product"], float(row["qty"]), float(row["price"]), float(row["fee"])
        last_fill[p] = px
        lot = lots.setdefault(p, [0.0, 0.0])
        if row["side"] == "BUY":
            lot[0] += q
            lot[1] += q * px + fee
        elif lot[0] > 0:
            part = lot[1] * min(1.0, q / lot[0])
            realized += q * px - fee - part
            lot[0] -= q
            lot[1] -= part

    holdings = state.get("holdings", {})
    prices = dict(state.get("prices") or {})
    source = "last hourly mark"
    missing = [p for p in holdings if p not in prices]
    if missing and fallback_prices:
        try:
            prices.update(fallback_prices(missing))
            source = "live public price"
        except Exception:
            pass
    rows = []
    for p, q in holdings.items():
        px = prices.get(p)
        src = source
        if px is None:
            px, src = last_fill.get(p, 0.0), "last fill (no mark yet)"
        cost = lots.get(p, [0.0, 0.0])[1]
        rows.append({"product": p, "qty": q, "price": px, "price_source": src, "value": q * px, "cost": cost,
                     "pnl": q * px - cost})
    cash = float(state.get("cash", 0.0))
    if abs(cash) < 1e-6:
        cash = 0.0
    equity = cash + sum(r["value"] for r in rows)
    for r in rows:
        r["weight"] = r["value"] / equity if equity else 0.0
    series = [[int(t), float(v)] for t, v in state.get("equity", [])]
    return {
        "name": name,
        "description": book.description if book else "",
        "rebalance_hours": book.rebalance_hours if book else None,
        "start": start,
        "equity": equity,
        "pnl": equity - start,
        "pct": (equity / start - 1) * 100 if start else 0.0,
        "cash": cash,
        "fees": float(state.get("fees", 0.0)),
        "realized": realized,
        "unrealized": sum(r["pnl"] for r in rows),
        "holdings": sorted(rows, key=lambda r: -r["value"]),
        "series": series,
        "last_mark": series[-1][0] if series else None,
        "last_rebalance": int(state.get("last_rebalance") or 0) or None,
        "peak": max([v for _, v in series] + [start]),
    }


def _runs(raw: dict | list | None) -> list[dict]:
    items = raw.get("workflow_runs", []) if isinstance(raw, dict) else (raw or [])
    out = []
    for r in items:
        started = _ts(r.get("run_started_at") or r.get("created_at"))
        if started is None:
            continue
        ended = _ts(r.get("updated_at")) if r.get("status") == "completed" else None
        out.append({
            "number": r.get("run_number"),
            "event": r.get("event"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion") or r.get("status"),
            "started": started,
            "duration": (ended - started) if ended else None,
            "url": r.get("html_url"),
        })
    return sorted(out, key=lambda r: -r["started"])


def health(runs: list[dict], marks: list[int], now: int, window_hours: int = 48) -> dict:
    """Hour-by-hour view of whether the workflow ran, plus headline numbers. Falls back to book marks without runs."""
    starts = sorted(r["started"] for r in runs) or sorted(set(marks))
    first = starts[0] if starts else now
    by_hour: dict[int, list[dict]] = {}
    for r in runs:
        by_hour.setdefault(r["started"] // HOUR, []).append(r)
    mark_hours = {m // HOUR for m in marks}
    cells = []
    this_hour = now // HOUR
    for h in range(this_hour - window_hours + 1, this_hour + 1):
        if (h + 1) * HOUR <= first:
            state = "before"
        elif h in by_hour:
            cons = {r["conclusion"] for r in by_hour[h]}
            state = "fail" if cons & {"failure", "timed_out", "startup_failure"} else \
                "ok" if "success" in cons else "running" if cons & {"in_progress", "queued"} else "other"
        elif not runs and h in mark_hours:
            state = "ok"
        elif h == this_hour:
            state = "pending"
        else:
            state = "missed"
        cells.append({"hour": h * HOUR, "state": state, "runs": [r["number"] for r in by_hour.get(h, [])]})

    last24 = [c for c in cells[-25:-1] if c["state"] != "before"]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    done = [r for r in runs if r["status"] == "completed"]
    ok = [r for r in done if r["conclusion"] == "success"]
    last_ok = max((r["started"] for r in ok), default=None) if runs else (max(marks) if marks else None)
    since = now - last_ok if last_ok else None
    failed_last = bool(done) and done[0]["conclusion"] not in ("success", "skipped", "cancelled")
    missed = sum(c["state"] == "missed" for c in last24)
    durations = [r["duration"] for r in done if r["duration"] is not None]
    if since is None or since > 3 * HOUR or failed_last:
        level = "critical"
    elif since > 90 * 60 or missed > 3:
        level = "warning"
    else:
        level = "good"
    return {
        "level": level,
        "since_last_ok": since,
        "last_ok": last_ok,
        "failed_last": failed_last,
        "hours_checked": len(last24),
        "hours_with_run": sum(c["state"] in ("ok", "fail", "running", "other") for c in last24),
        "missed_24h": missed,
        "success_rate": len(ok) / len(done) if done else None,
        "completed": len(done),
        "longest_gap": max(gaps) if gaps else None,
        "median_duration": statistics.median(durations) if durations else None,
        "source": "GitHub Actions runs" if runs else "book marks (no runs file)",
        "cells": cells,
    }


def collect(data_dir: Path, runs_raw: dict | list | None = None, repo: str = "", now: int | None = None,
            fallback_prices: Callable[[list[str]], dict] | None = None) -> dict:
    now = int(now or time.time())
    journal = _journal(data_dir)
    books = []
    for path in sorted(data_dir.glob("rotation_*.json")):
        name = path.stem.removeprefix("rotation_")
        books.append(_book(name, json.loads(path.read_text(encoding="utf-8")), journal, fallback_prices))
    order = {"fast": 0, "daily": 1}
    books.sort(key=lambda b: (order.get(b["name"], 9), b["name"]))

    fills = [{"t": r["t"], "book": r["book"], "side": r["side"], "product": r["product"], "qty": r["qty"],
              "price": r["price"], "value": r["qty"] * r["price"], "fee": r["fee"]}
             for r in journal if r.get("event") == "fill"]
    rebalances, fills_since = [], {}  # a rebalance event follows the fills it made
    for r in journal:
        if r.get("event") == "fill":
            fills_since[r["book"]] = fills_since.get(r["book"], 0) + 1
        elif r.get("event") == "rebalance":
            rebalances.append({"t": r["t"], "book": r["book"], "note": r.get("note", ""),
                               "equity": r.get("equity"), "trades": fills_since.pop(r["book"], 0)})
    errors = [{"t": r["t"], "book": r.get("book"), "error": r.get("error", "")}
              for r in journal if r.get("event") == "error"]
    runs = _runs(runs_raw)
    marks = sorted({t for b in books for t, _ in b["series"]})
    return {
        "generated": now,
        "repo": repo,
        "books": books,
        "fills": sorted(fills, key=lambda f: -(f["t"] or 0)),
        "rebalances": sorted(rebalances, key=lambda r: -(r["t"] or 0)),
        "errors": sorted(errors, key=lambda e: -(e["t"] or 0)),
        "runs": runs,
        "health": health(runs, marks, now),
    }


def render(data: dict) -> str:
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", blob)


def _live_prices(products: list[str]) -> dict:
    from .market_data import fetch_candles
    out = {}
    for p in products:
        bars = fetch_candles(p, "ONE_HOUR", 2)
        if bars:
            out[p] = bars[-1].close
    return out


def write(data_dir: Path, out: str | Path | None = None, runs_path: str | Path | None = None,
          repo: str = "", live_fallback: bool = True) -> Path:
    data_dir = Path(data_dir)
    runs_file = Path(runs_path) if runs_path else data_dir / "workflow_runs.json"
    runs_raw = None
    if runs_file.exists():
        try:
            runs_raw = json.loads(runs_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            runs_raw = None
    data = collect(data_dir, runs_raw, repo, fallback_prices=_live_prices if live_fallback else None)
    target = Path(out) if out else data_dir / "dashboard" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(data), encoding="utf-8")
    return target


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper trading dashboard</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f4f2; --surface: #fcfcfb; --border: #e3e2de; --grid: #ecebe8;
  --text: #0b0b0b; --text-2: #52514e; --muted: #7a7974;
  --s1: #2a78d6; --s2: #eb6834;
  --good: #0ca30c; --warning: #b77f00; --critical: #d03b3b; --neutral: #b9b8b2; --before: #ecebe8;
  --pos: #0b7a0b; --neg: #c02f2f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #111110; --surface: #1a1a19; --border: #2c2c2a; --grid: #262624;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
    --s1: #3987e5; --s2: #d95926;
    --good: #0ca30c; --warning: #fab219; --critical: #e66767; --neutral: #4a4a46; --before: #222220;
    --pos: #3cc43c; --neg: #ef7b7b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #111110; --surface: #1a1a19; --border: #2c2c2a; --grid: #262624;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e86;
  --s1: #3987e5; --s2: #d95926;
  --good: #0ca30c; --warning: #fab219; --critical: #e66767; --neutral: #4a4a46; --before: #222220;
  --pos: #3cc43c; --neg: #ef7b7b;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-variant-numeric: tabular-nums; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 16px; }
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 15px; margin: 0 0 12px; }
.sub { color: var(--text-2); font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; min-width: 0; }
.grid { display: grid; gap: 16px; }
.g2 { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile .k { color: var(--text-2); font-size: 12px; }
.tile .v { font-size: 22px; font-weight: 600; margin-top: 2px; }
.tile .d { color: var(--muted); font-size: 12px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
.pos { color: var(--pos); } .neg { color: var(--neg); }
.book-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.book-head .eq { font-size: 26px; font-weight: 650; }
.swatch { width: 12px; height: 3px; border-radius: 2px; display: inline-block; vertical-align: middle; margin-right: 6px; }
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin: 10px 0 12px; }
.kv div span { display: block; color: var(--text-2); font-size: 12px; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--text-2); font-weight: 500; font-size: 12px; }
td.n, th.n { text-align: right; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: var(--text-2); margin-bottom: 8px; }
.chart { position: relative; }
.chart svg { display: block; width: 100%; height: 260px; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; white-space: nowrap; z-index: 2; }
.strip { display: grid; grid-template-columns: repeat(48, 1fr); gap: 2px; margin: 8px 0 4px; }
.cell { height: 22px; border-radius: 3px; position: relative; }
.cell.missed { background: repeating-linear-gradient(135deg, var(--critical) 0 2px, transparent 2px 5px); outline: 1px solid var(--critical); outline-offset: -1px; opacity: .8; }
.strip-axis { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }
.keyrow { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--text-2); margin-top: 8px; }
.keyrow .cell { width: 14px; height: 14px; display: inline-block; vertical-align: -3px; margin-right: 5px; }
.filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.filters button { font: inherit; font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border);
  background: transparent; color: var(--text-2); cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--text); color: var(--surface); border-color: var(--text); }
.note { color: var(--text-2); font-size: 12px; margin-top: 8px; }
.empty { color: var(--muted); padding: 8px 0; }
a { color: var(--s1); }
.badge { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-2); }
@media (max-width: 600px) { .strip { gap: 1px; } .cell { height: 18px; } .book-head .eq { font-size: 22px; } }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Paper trading dashboard</h1>
      <div class="sub">Simulated $1,000 books run by the hourly <code>paper-trading</code> workflow. No real orders; descriptive only.</div>
    </div>
    <div class="sub" id="generated"></div>
  </header>

  <section class="card" id="health"></section>
  <section class="card">
    <h2>Equity over time</h2>
    <div class="legend" id="legend"></div>
    <div class="chart" id="equity"><div class="tip"></div></div>
  </section>
  <section class="grid g2" id="books"></section>
  <section class="card">
    <h2>Rebalances</h2>
    <div class="scroll" id="rebalances"></div>
  </section>
  <section class="card">
    <h2>Trades</h2>
    <div class="filters" id="fill-filters"></div>
    <div class="scroll" id="fills"></div>
  </section>
  <section class="card">
    <h2>Workflow runs</h2>
    <div class="scroll" id="runs"></div>
  </section>
  <section class="card" id="errors-card" hidden>
    <h2>Book errors</h2>
    <div class="scroll" id="errors"></div>
  </section>
</main>
<script>
const D = __DATA__;
const COLORS = {fast: "var(--s1)", daily: "var(--s2)"};
const color = n => COLORS[n] || "var(--muted)";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (v, d = 2) => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
const signed = v => (v > 0 ? "+" : "") + usd(v);
const pct = v => (v > 0 ? "+" : "") + v.toFixed(2) + "%";
const cls = v => v > 0.005 ? "pos" : v < -0.005 ? "neg" : "";
const num = v => v >= 1000 ? v.toLocaleString(undefined, {maximumFractionDigits: 2}) : v >= 1 ? v.toFixed(4) : v.toPrecision(4);
const when = t => t ? new Date(t * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "—";
const ago = s => s == null ? "—" : s < 3600 ? Math.round(s / 60) + " min" : s < 172800 ? (s / 3600).toFixed(1) + " h" : Math.round(s / 86400) + " days";
const coin = p => p.split("-")[0];
const table = (head, rows, empty) => rows.length
  ? `<table><thead><tr>${head.map(h => `<th class="${h[1] || ""}">${h[0]}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`
  : `<div class="empty">${empty}</div>`;

$("generated").textContent = "Updated " + when(D.generated);

// ---- health
(() => {
  const h = D.health;
  const label = {good: "Healthy", warning: "Needs a look", critical: "Not running"}[h.level];
  const icon = {good: "✓", warning: "!", critical: "✕"}[h.level];
  const tiles = [
    ["Last successful run", h.since_last_ok == null ? "never" : ago(h.since_last_ok) + " ago", when(h.last_ok)],
    ["Hours with a run, last 24", `${h.hours_with_run} / ${h.hours_checked}`, `${h.missed_24h} hourly runs missed`],
    ["Success rate", h.success_rate == null ? "—" : Math.round(h.success_rate * 100) + "%", `${h.completed} completed runs`],
    ["Longest gap between runs", ago(h.longest_gap), h.median_duration ? `runs take about ${Math.round(h.median_duration)} s` : ""],
  ];
  const fill = {ok: "var(--good)", fail: "var(--critical)", running: "var(--warning)", other: "var(--neutral)", pending: "var(--neutral)", before: "var(--before)"};
  const words = {ok: "ran OK", fail: "failed", running: "running", other: "cancelled or skipped", pending: "this hour, not run yet", missed: "no run (missed)", before: "before the first run"};
  const cells = h.cells.map(c => `<div class="cell ${c.state}" style="${c.state === "missed" ? "" : "background:" + fill[c.state]}"
    data-tip="${esc(when(c.hour) + ": " + words[c.state] + (c.runs.length ? " (run #" + c.runs.join(", #") + ")" : ""))}"></div>`).join("");
  $("health").innerHTML = `
    <div class="book-head"><h2>Workflow health</h2>
      <span class="status" style="color:var(--${h.level === "good" ? "pos" : h.level})"><span class="dot" style="background:var(--${h.level})"></span>${icon} ${label}</span></div>
    <div class="tiles">${tiles.map(t => `<div class="tile"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="d">${t[2]}</div></div>`).join("")}</div>
    <div class="note" style="margin-top:14px">Last 48 hours, one cell per hour (scheduled at :15 past each hour). Hover a cell for details.</div>
    <div class="chart"><div class="strip">${cells}</div><div class="tip"></div></div>
    <div class="strip-axis"><span>${when(h.cells[0].hour)}</span><span>now</span></div>
    <div class="keyrow">${["ok", "fail", "missed", "pending", "before"].map(s => `<span><span class="cell ${s}" style="${s === "missed" ? "" : "background:" + fill[s]}"></span>${words[s]}</span>`).join("")}</div>
    <div class="note">Source: ${esc(h.source)}. GitHub can delay or drop scheduled runs on busy hours; a missed hour means the fast book can rebalance late.</div>`;
  const wrap = $("health").querySelector(".chart"), tip = wrap.querySelector(".tip");
  wrap.addEventListener("mousemove", e => {
    const c = e.target.closest(".cell");
    if (!c) { tip.style.display = "none"; return; }
    tip.textContent = c.dataset.tip; tip.style.display = "block";
    const r = wrap.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth) + "px"; tip.style.top = "30px";
  });
  wrap.addEventListener("mouseleave", () => tip.style.display = "none");
})();

// ---- equity chart
(() => {
  const books = D.books.filter(b => b.series.length);
  const box = $("equity"), tip = box.querySelector(".tip");
  $("legend").innerHTML = books.map(b => `<span><span class="swatch" style="background:${color(b.name)}"></span>${esc(b.name)} book</span>`).join("")
    + `<span><span class="swatch" style="background:var(--muted);height:1px"></span>$1,000 start</span>`;
  if (!books.length) { box.insertAdjacentHTML("beforeend", `<div class="empty">No equity history yet.</div>`); return; }
  const draw = () => {
  box.querySelector("svg")?.remove();
  const W = Math.max(box.clientWidth, 280), H = 260, m = {l: 56, r: 52, t: 12, b: 28};
  const all = books.flatMap(b => b.series);
  let t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
  if (t1 - t0 < 3600) { t0 -= 1800; t1 += 1800; }
  let lo = Math.min(1000, ...all.map(p => p[1])), hi = Math.max(1000, ...all.map(p => p[1]));
  const pad = Math.max((hi - lo) * 0.15, 5); lo -= pad; hi += pad;
  const x = t => m.l + (t - t0) / (t1 - t0) * (W - m.l - m.r);
  const y = v => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500].find(s => (hi - lo) / s <= 6) || 1000;
  let g = "";
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step)
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${usd(v, 0)}</text>`;
  g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(1000)}" y2="${y(1000)}" stroke="var(--muted)" stroke-dasharray="4 4"/>`;
  const ticks = W < 560 ? 2 : 5;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + (t1 - t0) * i / ticks;
    g += `<text x="${x(t)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}" font-size="11" fill="var(--muted)">${new Date(t * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric"})}</text>`;
  }
  for (const b of books) {
    const pts = b.series.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
    g += `<polyline points="${pts}" fill="none" stroke="${color(b.name)}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    if (b.series.length <= 60) g += b.series.map(p => `<circle cx="${x(p[0])}" cy="${y(p[1])}" r="4" fill="${color(b.name)}" stroke="var(--surface)" stroke-width="2"/>`).join("");
    const last = b.series[b.series.length - 1];
    g += `<text x="${x(last[0]) + 8}" y="${y(last[1]) + 4}" font-size="12" fill="var(--text-2)">${esc(b.name)}</text>`;
  }
  g += `<line class="xh" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" visibility="hidden"/>`;
  box.insertAdjacentHTML("afterbegin", `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Equity of each paper book over time">${g}</svg>`);
  const svg = box.querySelector("svg"), xh = svg.querySelector(".xh");
  svg.addEventListener("mousemove", e => {
    const r = svg.getBoundingClientRect(), tx = (e.clientX - r.left) / r.width * W;
    const t = t0 + (tx - m.l) / (W - m.l - m.r) * (t1 - t0);
    const near = books.map(b => [b, b.series.reduce((a, p) => Math.abs(p[0] - t) < Math.abs(a[0] - t) ? p : a)]);
    const at = near[0][1][0];
    xh.setAttribute("x1", x(at)); xh.setAttribute("x2", x(at)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = `<div style="color:var(--text-2);margin-bottom:4px">${when(at)}</div>` + near.map(([b, p]) =>
      `<div><span class="swatch" style="background:${color(b.name)}"></span>${esc(b.name)} <b>${usd(p[1])}</b> <span class="${cls(p[1] - b.start)}">${pct((p[1] / b.start - 1) * 100)}</span></div>`).join("");
    tip.style.display = "block";
    const px = x(at) / W * r.width;
    tip.style.left = (px + tip.offsetWidth + 16 > r.width ? px - tip.offsetWidth - 12 : px + 12) + "px"; tip.style.top = "8px";
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); });
  };
  draw();
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(draw, 150); });
})();

// ---- books
$("books").innerHTML = D.books.map(b => {
  const rows = b.holdings.map(h => `<tr><td>${esc(coin(h.product))}</td><td class="n">${num(h.qty)}</td><td class="n" title="${esc(h.price_source)}">${num(h.price)}</td>
    <td class="n">${usd(h.value)}</td><td class="n">${(h.weight * 100).toFixed(1)}%</td><td class="n">${usd(h.cost)}</td><td class="n ${cls(h.pnl)}">${signed(h.pnl)}</td></tr>`);
  if (b.cash > 0.005) rows.push(`<tr><td>Cash</td><td></td><td></td><td class="n">${usd(b.cash)}</td><td class="n">${(b.cash / b.equity * 100).toFixed(1)}%</td><td></td><td></td></tr>`);
  const dd = b.peak > 0 ? (b.equity / b.peak - 1) * 100 : 0;
  const src = [...new Set(b.holdings.map(h => h.price_source))].join(", ");
  return `<div class="card">
    <div class="book-head"><h2><span class="swatch" style="background:${color(b.name)}"></span>${esc(b.name)} book</h2>
      <span class="badge">rebalances every ${b.rebalance_hours} h</span></div>
    <div class="sub">${esc(b.description)}</div>
    <div class="book-head" style="margin-top:10px"><span class="eq">${usd(b.equity)}</span><span class="${cls(b.pnl)}" style="font-weight:600">${signed(b.pnl)} (${pct(b.pct)})</span></div>
    <div class="kv">
      <div><span>Unrealized</span><b class="${cls(b.unrealized)}">${signed(b.unrealized)}</b></div>
      <div><span>Realized</span><b class="${cls(b.realized)}">${signed(b.realized)}</b></div>
      <div><span>Fees paid</span><b>${usd(b.fees)}</b></div>
      <div><span>From peak</span><b class="${cls(dd)}">${pct(dd)}</b></div>
      <div><span>Last rebalance</span><b>${when(b.last_rebalance)}</b></div>
      <div><span>Last marked</span><b>${when(b.last_mark)}</b></div>
    </div>
    <div class="scroll">${table([["Coin"], ["Qty", "n"], ["Price", "n"], ["Value", "n"], ["Weight", "n"], ["Cost", "n"], ["P&amp;L", "n"]], rows, "All cash.")}</div>
    ${src ? `<div class="note">Prices: ${esc(src)}. Cost includes the 0.5% simulated fee.</div>` : ""}
  </div>`;
}).join("") || `<div class="card empty">No paper books found in data/.</div>`;

// ---- rebalances
$("rebalances").innerHTML = table([["When"], ["Book"], ["Trades", "n"], ["Equity after", "n"], ["Picks (score)"]],
  D.rebalances.map(r => `<tr><td>${when(r.t)}</td><td><span class="swatch" style="background:${color(r.book)}"></span>${esc(r.book)}</td>
    <td class="n">${r.trades}</td><td class="n">${r.equity == null ? "—" : usd(r.equity)}</td><td>${esc(r.note.replace(/^picks: /, "").replace(/-USDC/g, ""))}</td></tr>`),
  "No rebalances yet.") + `<div class="note">A rebalance with 0 trades kept the same coins; changes under 2% of equity are skipped to save fees.</div>`;

// ---- fills
(() => {
  let book = "all";
  const names = ["all", ...new Set(D.fills.map(f => f.book))];
  const draw = () => {
    $("fill-filters").innerHTML = names.map(n => `<button aria-pressed="${n === book}" data-b="${esc(n)}">${n === "all" ? "All books" : esc(n)}</button>`).join("");
    const rows = D.fills.filter(f => book === "all" || f.book === book).map(f => `<tr><td>${when(f.t)}</td>
      <td><span class="swatch" style="background:${color(f.book)}"></span>${esc(f.book)}</td>
      <td>${f.side === "BUY" ? "Buy" : "Sell"}</td><td>${esc(coin(f.product))}</td><td class="n">${num(f.qty)}</td>
      <td class="n">${num(f.price)}</td><td class="n">${usd(f.value)}</td><td class="n">${usd(f.fee)}</td></tr>`);
    $("fills").innerHTML = table([["When"], ["Book"], ["Side"], ["Coin"], ["Qty", "n"], ["Fill price", "n"], ["Value", "n"], ["Fee", "n"]], rows, "No trades yet.");
  };
  $("fill-filters").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { book = b.dataset.b; draw(); } });
  draw();
})();

// ---- runs
(() => {
  const st = {success: ["good", "✓ success"], failure: ["critical", "✕ failed"], timed_out: ["critical", "✕ timed out"],
    cancelled: ["neutral", "– cancelled"], in_progress: ["warning", "… running"], queued: ["warning", "… queued"]};
  const rows = D.runs.slice(0, 100).map(r => { const s = st[r.conclusion] || ["neutral", r.conclusion];
    return `<tr><td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">#${r.number}</a>` : "#" + r.number}</td><td>${when(r.started)}</td>
      <td>${r.event === "schedule" ? "Scheduled" : r.event === "workflow_dispatch" ? "Manual" : esc(r.event)}</td>
      <td><span class="status" style="font-weight:500"><span class="dot" style="background:var(--${s[0]})"></span>${esc(s[1])}</span></td>
      <td class="n">${r.duration == null ? "—" : r.duration + " s"}</td></tr>`; });
  const link = D.repo ? ` <a href="https://github.com/${esc(D.repo)}/actions/workflows/paper-trading.yml" target="_blank" rel="noopener">All runs on GitHub</a>` : "";
  $("runs").innerHTML = table([["Run"], ["Started"], ["Trigger"], ["Result"], ["Duration", "n"]], rows,
    "No run history in this build (it is fetched when the page is built by the workflow).") + `<div class="note">${D.runs.some(r => r.status !== "completed") ? "The run still in progress is the one that built this page. " : ""}${link}</div>`;
})();

// ---- errors
if (D.errors.length) {
  $("errors-card").hidden = false;
  $("errors").innerHTML = table([["When"], ["Book"], ["Error"]], D.errors.slice(0, 50).map(e => `<tr><td>${when(e.t)}</td><td>${esc(e.book)}</td><td>${esc(e.error)}</td></tr>`), "");
}
</script>
</body>
</html>
"""
