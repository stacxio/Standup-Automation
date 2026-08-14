"""Scorecard dashboard — turn the fact table into a self-contained HTML page.

Pure, like `scorecard.py` and `archive.py`: no Sheets, Slack or network calls.
`build_dashboard.py` reads the 'Scorecard Daily' tab and hands the rows here.

The output is one file with no external references — no CDN, no fonts, no
images — so it can be opened from disk, mailed, or published as-is. The daily
records are embedded as JSON and the page aggregates them in the browser, which
is what makes the period filter work without a server.

Panels, in the order a reader needs them:

  1. Hero + KPI row   the team's latest average, then process/delivery/coverage
  2. Stacked bars     process vs delivery per developer — shows *which half*
                      of the rubric someone is failing, which a single total
                      cannot
  3. Trend line       team average over the period; trajectory beats any one day
  4. Heatmap          developer x day, so gaps and patterns are visible at once
  5. Table            every value in text form — the accessible twin, and the
                      place `Not Scored` days are named rather than just blank

Colour follows docs/SCORING.md's split: process and delivery are two
*identities*, not two magnitudes, so they get the first two categorical slots
(validated for colour-vision deficiency in both light and dark). The heatmap is
a single-hue sequential ramp because its job is magnitude. `Not Scored` is
rendered as an empty cell, never as a zero — the same distinction the scorer
makes.
"""

from __future__ import annotations

import datetime as dt
import json

TITLE = "Team Scorecard"

# Columns this page needs out of scorecard.DAILY_HEADERS. Named rather than
# positional so a new column in the fact table cannot silently shift the parse.
_FIELDS = ("Date", "Developer", "Attendance", "Status", "Reason",
           "Tasks Picked", "Tasks Done", "Process", "Delivery", "Total",
           "Band", "Flags")


def _number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def records_from_rows(rows: list[list[str]], headers: list[str]) -> list[dict]:
    """Parse 'Scorecard Daily' rows into the records the page renders.

    Rows whose date will not parse are dropped rather than guessed at: a
    malformed row is a data fault, and inventing a date for it would put a
    score on the wrong day.
    """
    index = {name: headers.index(name) for name in _FIELDS if name in headers}
    if "Date" not in index or "Developer" not in index:
        return []

    def cell(row: list[str], name: str) -> str:
        position = index.get(name)
        return row[position] if position is not None and position < len(row) else ""

    records = []
    for row in rows:
        if not row or len(row) <= index["Developer"]:
            continue
        try:
            date = dt.date.fromisoformat(cell(row, "Date")).isoformat()
        except ValueError:
            continue
        if not cell(row, "Developer"):
            continue
        records.append({
            "date": date,
            "developer": cell(row, "Developer"),
            "attendance": cell(row, "Attendance"),
            "status": cell(row, "Status"),
            "reason": cell(row, "Reason"),
            "picked": int(_number(cell(row, "Tasks Picked")) or 0),
            "done": int(_number(cell(row, "Tasks Done")) or 0),
            "process": _number(cell(row, "Process")),
            "delivery": _number(cell(row, "Delivery")),
            "total": _number(cell(row, "Total")),
            "band": cell(row, "Band"),
            "flags": [f.strip() for f in cell(row, "Flags").split(",") if f.strip()],
        })
    records.sort(key=lambda r: (r["date"], r["developer"]))
    return records


def render(records: list[dict], *, generated_at: dt.datetime,
           title: str = TITLE, note: str = "") -> str:
    """The complete page. `note` renders as a banner above everything."""
    payload = {
        "records": records,
        "generated": generated_at.strftime("%d %b %Y, %H:%M"),
        "note": note,
    }
    return (_PAGE
            .replace("__TITLE__", _escape(title))
            .replace("__DATA__", _json_for_script(payload)))


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _json_for_script(payload: dict) -> str:
    """JSON safe to embed inside a <script> block.

    `json.dumps` escapes nothing HTML-significant, so a developer whose Slack
    display name contained `</script>` would close the tag and everything after
    it would render as markup. Names reach this page from Slack profiles, which
    are user-controlled, so this is an injection vector rather than a
    theoretical one. Escaping the three characters as `\\uXXXX` keeps the JSON
    valid and identical once parsed.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    return (raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))


# --------------------------------------------------------------------------
# The page. Content-only (no <html>/<body> wrapper) so the same output can be
# opened from disk and published as an artifact unchanged.
# --------------------------------------------------------------------------
_PAGE = r"""<title>__TITLE__</title>
<style>
  /* Light is the base declaration; dark redefines only the tokens, under both
     the OS media query and the explicit theme stamp, so a viewer's toggle wins
     in either direction. */
  :root {
    color-scheme: light;
    --plane: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11, 11, 11, 0.10);
    --series-1: #2a78d6;   /* process  — categorical slot 1 */
    --series-2: #eb6834;   /* delivery — categorical slot 2 */
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
    --seq-0: #cde2fb; --seq-1: #9ec5f4; --seq-2: #6da7ec; --seq-3: #3987e5;
    --seq-4: #256abf; --seq-5: #184f95; --seq-6: #0d366b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255, 255, 255, 0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --plane: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
  }

  body {
    margin: 0;
    background: var(--plane);
    color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 72px; }
  /* Siblings are spaced by the container, so nothing depends on collapsing margins. */
  .stack { display: flex; flex-direction: column; gap: 16px; }

  :where(a, button, [tabindex]):focus-visible {
    outline: 2px solid var(--series-1);
    outline-offset: 2px;
    border-radius: 3px;
  }
  @media (prefers-reduced-motion: reduce) {
    * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
  }

  h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--ink-2); font-size: 13px; margin: 0; }
  h2 { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
  .cap { color: var(--muted); font-size: 12.5px; margin: 0 0 18px; }

  .banner {
    margin: 20px 0 0; padding: 10px 14px; border-radius: 8px;
    background: color-mix(in srgb, var(--warning) 14%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent);
    color: var(--ink); font-size: 13px;
  }

  /* One filter row above everything it scopes — never per-card. */
  .filters { display: flex; gap: 8px; align-items: center; margin: 22px 0 20px; flex-wrap: wrap; }
  .filters .lbl { color: var(--muted); font-size: 12.5px; margin-right: 2px; }
  .chip {
    font: inherit; font-size: 13px; cursor: pointer;
    padding: 5px 12px; border-radius: 999px;
    background: var(--surface); color: var(--ink-2);
    border: 1px solid var(--border);
  }
  .chip:hover { color: var(--ink); }
  .chip[aria-pressed="true"] { background: var(--ink); color: var(--plane); border-color: var(--ink); }

  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px 20px;
  }

  /* Hero + stat tiles */
  .hero-card { display: flex; flex-wrap: wrap; gap: 28px; align-items: flex-end; }
  .hero .label { color: var(--muted); font-size: 12.5px; margin-bottom: 2px; }
  .hero .value { font-size: 54px; font-weight: 600; line-height: 1; letter-spacing: -0.02em; }
  .hero .value small { font-size: 20px; font-weight: 500; color: var(--muted); }
  .hero .band { font-size: 13px; color: var(--ink-2); margin-top: 6px; }
  .tiles { display: flex; gap: 26px; flex-wrap: wrap; margin-left: auto; }
  .tile .label { color: var(--muted); font-size: 12.5px; margin-bottom: 3px; }
  .tile .value { font-size: 22px; font-weight: 600; }
  .tile .value small { font-size: 13px; font-weight: 500; color: var(--muted); }

  /* Legend — always present for two or more series */
  .legend { display: flex; gap: 16px; margin: 0 0 14px; font-size: 12.5px; color: var(--ink-2); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .key { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

  /* Stacked bars */
  .bars { display: flex; flex-direction: column; gap: 10px; }
  .bar-row { display: grid; grid-template-columns: 116px 1fr 52px; gap: 12px; align-items: center; }
  .bar-name { font-size: 13px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-track { height: 18px; background: var(--grid); border-radius: 3px; display: flex; overflow: hidden; }
  .seg { height: 100%; }
  .seg-1 { background: var(--series-1); }
  /* The 2px surface gap is what separates the segments — never a border. */
  .seg-2 { background: var(--series-2); border-left: 2px solid var(--surface); border-radius: 0 4px 4px 0; }
  .bar-val { font-size: 13px; text-align: right; font-variant-numeric: tabular-nums; color: var(--ink); }
  .bar-none { font-size: 12.5px; color: var(--muted); grid-column: 2 / 4; }

  /* Trend */
  .plot { width: 100%; display: block; overflow: visible; }
  .plot .gl { stroke: var(--grid); stroke-width: 1; }
  .plot .ax { stroke: var(--axis); stroke-width: 1; }
  .plot .ln { fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .plot .dot { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }
  .plot text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .plot .hit { fill: transparent; cursor: pointer; }
  .plot .cross { stroke: var(--axis); stroke-width: 1; opacity: 0; }

  /* Heatmap */
  .heat-scroll { overflow-x: auto; }
  .heat { border-collapse: separate; border-spacing: 2px; font-size: 11px; }
  .heat th { font-weight: 500; color: var(--muted); padding: 0 4px; text-align: right; white-space: nowrap; }
  .heat thead th { text-align: center; font-variant-numeric: tabular-nums; }
  .heat td { width: 22px; height: 22px; border-radius: 3px; background: var(--grid); }
  .heat td.v { cursor: pointer; }
  .scale { display: flex; align-items: center; gap: 8px; margin-top: 14px; font-size: 12px; color: var(--muted); }
  .scale i { width: 20px; height: 10px; border-radius: 2px; display: inline-block; }

  /* Table — the accessible twin; every value is reachable here */
  .table-scroll { overflow-x: auto; }
  table.data { border-collapse: collapse; width: 100%; font-size: 13px; }
  table.data th, table.data td { padding: 8px 10px; text-align: right; white-space: nowrap; }
  table.data th:first-child, table.data td:first-child { text-align: left; }
  table.data thead th { color: var(--muted); font-weight: 500; font-size: 12px; border-bottom: 1px solid var(--border); }
  table.data tbody tr + tr td { border-top: 1px solid var(--grid); }
  table.data td.num { font-variant-numeric: tabular-nums; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; vertical-align: 1px; }
  .muted { color: var(--muted); }

  #tip {
    position: fixed; z-index: 20; pointer-events: none; opacity: 0;
    transform: translate(-50%, -100%); transition: opacity .1s;
    background: var(--ink); color: var(--plane);
    padding: 7px 10px; border-radius: 7px; font-size: 12px; line-height: 1.45;
    max-width: 260px; box-shadow: 0 4px 14px rgba(0,0,0,.18);
  }
  #tip b { font-weight: 600; }

  @media (max-width: 560px) {
    .bar-row { grid-template-columns: 84px 1fr 46px; }
    .hero .value { font-size: 42px; }
    .tiles { margin-left: 0; gap: 20px; }
  }
</style>

<div class="wrap">
  <h1>__TITLE__</h1>
  <p class="sub" id="sub"></p>
  <div id="banner"></div>

  <div class="filters" role="group" aria-label="Period">
    <span class="lbl">Period</span>
    <button class="chip" data-days="7">Last 7 days</button>
    <button class="chip" data-days="14">Last 14 days</button>
    <button class="chip" data-days="30" aria-pressed="true">Last 30 days</button>
  </div>

  <div class="stack">
  <div class="card hero-card">
    <div class="hero">
      <div class="label" id="heroLabel">Team average</div>
      <div class="value" id="heroVal">—</div>
      <div class="band" id="heroBand"></div>
    </div>
    <div class="tiles" id="tiles"></div>
  </div>

  <div class="card">
    <h2>Process vs delivery</h2>
    <p class="cap">Average points per developer over the period. Process is out of 40, delivery out of 60.</p>
    <div class="legend">
      <span><i class="key" style="background:var(--series-1)"></i> Process (40)</span>
      <span><i class="key" style="background:var(--series-2)"></i> Delivery (60)</span>
    </div>
    <div class="bars" id="bars"></div>
  </div>

  <div class="card">
    <h2>Team average over time</h2>
    <p class="cap">One point per day. Days where nobody was scored are skipped, not drawn as zero.</p>
    <svg class="plot" id="trend" viewBox="0 0 720 220" preserveAspectRatio="xMidYMid meet" role="img"
         aria-label="Team average score per day"></svg>
  </div>

  <div class="card">
    <h2>Daily scores</h2>
    <p class="cap">Each cell is one developer-day. Empty cells are days that were not scored — leave, weekends, or missing data.</p>
    <div class="heat-scroll"><table class="heat" id="heat"></table></div>
    <div class="scale" id="scale"></div>
  </div>

  <div class="card">
    <h2>Per developer</h2>
    <p class="cap">Every value on this page in text form. Averages exclude days that were not scored; half days count half.</p>
    <div class="table-scroll"><table class="data" id="tbl"></table></div>
  </div>

  <div class="card">
    <h2>Days not scored</h2>
    <p class="cap">A missing fact is never a low score. These days are excluded from every average above.</p>
    <div class="table-scroll"><table class="data" id="unscored"></table></div>
  </div>
  </div>
</div>
<div id="tip" role="status"></div>

<script>
(function () {
  var DATA = __DATA__;
  var RECS = DATA.records || [];
  var SEQ = ['--seq-0','--seq-1','--seq-2','--seq-3','--seq-4','--seq-5','--seq-6'];
  var days = 30;

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  };
  var fmt = function (n) { return n == null ? '—' : (Math.round(n * 10) / 10).toFixed(1); };
  var scored = function (r) { return r.status === 'Scored' && r.total != null; };
  // Half days count half — the same weighting scorecard.period_average uses.
  var weight = function (r) { return /half\s*day/i.test(r.attendance || '') ? 0.5 : 1; };

  function average(rows) {
    var s = rows.filter(scored);
    if (!s.length) return null;
    var w = s.reduce(function (a, r) { return a + weight(r); }, 0);
    if (!w) return null;
    return s.reduce(function (a, r) { return a + r.total * weight(r); }, 0) / w;
  }
  function averageOf(rows, key) {
    var s = rows.filter(scored);
    if (!s.length) return null;
    return s.reduce(function (a, r) { return a + (r[key] || 0); }, 0) / s.length;
  }
  function bandOf(t) {
    if (t == null) return '';
    return t >= 85 ? 'Excellent' : t >= 70 ? 'On Track' : t >= 50 ? 'Needs Attention' : 'At Risk';
  }
  function bandColor(b) {
    return b === 'Excellent' ? 'var(--good)'
         : b === 'Needs Attention' ? 'var(--warning)'
         : b === 'At Risk' ? 'var(--critical)' : 'var(--axis)';
  }

  var tip = $('tip');
  function showTip(e, html) {
    tip.innerHTML = html;
    tip.style.opacity = '1';
    var r = e.currentTarget.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - 16, Math.max(16, r.left + r.width / 2)) + 'px';
    tip.style.top = (r.top - 8) + 'px';
  }
  function hideTip() { tip.style.opacity = '0'; }
  function hoverable(el, html) {
    el.tabIndex = 0;
    el.addEventListener('mouseenter', function (e) { showTip(e, html); });
    el.addEventListener('focus', function (e) { showTip(e, html); });
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('blur', hideTip);
  }

  function slice() {
    if (!RECS.length) return { rows: [], dates: [], devs: [] };
    var all = RECS.map(function (r) { return r.date; }).sort();
    var last = all[all.length - 1];
    var from = new Date(last + 'T00:00:00');
    from.setDate(from.getDate() - (days - 1));
    var cutoff = from.toISOString().slice(0, 10);
    var rows = RECS.filter(function (r) { return r.date >= cutoff && r.date <= last; });
    var dates = Array.from(new Set(rows.map(function (r) { return r.date; }))).sort();
    var devs = Array.from(new Set(rows.map(function (r) { return r.developer; }))).sort();
    return { rows: rows, dates: dates, devs: devs, last: last };
  }

  function renderHead(v) {
    $('sub').textContent = v.dates.length
      ? v.dates[0] + ' to ' + v.dates[v.dates.length - 1] + ' · ' + v.devs.length +
        ' developer' + (v.devs.length === 1 ? '' : 's') + ' · generated ' + DATA.generated
      : 'No data yet · generated ' + DATA.generated;
    if (DATA.note) {
      $('banner').innerHTML = '<div class="banner">' + esc(DATA.note) + '</div>';
    }
  }

  function renderKpis(v) {
    var latest = v.rows.filter(function (r) { return r.date === v.last; });
    var heroRows = latest.filter(scored).length ? latest : v.rows;
    var hero = average(heroRows);
    $('heroLabel').textContent = latest.filter(scored).length
      ? 'Team average — ' + v.last : 'Team average — period';
    $('heroVal').innerHTML = hero == null ? '—' : fmt(hero) + '<small>/100</small>';
    var band = bandOf(hero);
    $('heroBand').innerHTML = band
      ? '<i class="dot" style="background:' + bandColor(band) + '"></i>' + band : '';

    var s = v.rows.filter(scored);
    var absent = s.filter(function (r) { return /absent/i.test(r.attendance || ''); }).length;
    var unscored = v.rows.filter(function (r) { return r.status !== 'Scored'; }).length;
    var tiles = [
      ['Process', fmt(averageOf(v.rows, 'process')), '/40'],
      ['Delivery', fmt(averageOf(v.rows, 'delivery')), '/60'],
      ['Days scored', String(s.length), ''],
      ['Absences', String(absent), ''],
      ['Not scored', String(unscored), '']
    ];
    $('tiles').innerHTML = tiles.map(function (t) {
      return '<div class="tile"><div class="label">' + t[0] + '</div><div class="value">' +
        t[1] + '<small>' + t[2] + '</small></div></div>';
    }).join('');
  }

  function renderBars(v) {
    var host = $('bars');
    host.innerHTML = '';
    if (!v.devs.length) { host.innerHTML = '<p class="cap">Nothing to show.</p>'; return; }
    var per = v.devs.map(function (d) {
      var rows = v.rows.filter(function (r) { return r.developer === d; });
      return { dev: d, process: averageOf(rows, 'process'), delivery: averageOf(rows, 'delivery'),
               total: average(rows), n: rows.filter(scored).length };
    }).sort(function (a, b) { return (b.total || -1) - (a.total || -1); });

    per.forEach(function (p) {
      var row = document.createElement('div');
      row.className = 'bar-row';
      var name = document.createElement('div');
      name.className = 'bar-name';
      name.textContent = p.dev;
      row.appendChild(name);

      if (!p.n) {
        var none = document.createElement('div');
        none.className = 'bar-none';
        none.textContent = 'no scored days in this period';
        row.appendChild(none);
        host.appendChild(row);
        return;
      }
      var track = document.createElement('div');
      track.className = 'bar-track';
      var s1 = document.createElement('div');
      s1.className = 'seg seg-1';
      s1.style.width = (p.process || 0) + '%';
      hoverable(s1, '<b>' + esc(p.dev) + '</b><br>Process ' + fmt(p.process) + ' / 40');
      var s2 = document.createElement('div');
      s2.className = 'seg seg-2';
      s2.style.width = (p.delivery || 0) + '%';
      hoverable(s2, '<b>' + esc(p.dev) + '</b><br>Delivery ' + fmt(p.delivery) + ' / 60');
      track.appendChild(s1); track.appendChild(s2);
      row.appendChild(track);

      var val = document.createElement('div');
      val.className = 'bar-val';
      val.textContent = fmt(p.total);
      row.appendChild(val);
      host.appendChild(row);
    });
  }

  function renderTrend(v) {
    var svg = $('trend');
    var W = 720, H = 220, L = 34, R = 14, T = 12, B = 30;
    var pts = v.dates.map(function (d) {
      return { date: d, avg: average(v.rows.filter(function (r) { return r.date === d; })) };
    }).filter(function (p) { return p.avg != null; });

    if (pts.length < 2) {
      svg.innerHTML = '<text x="12" y="30">Not enough scored days to draw a trend.</text>';
      return;
    }
    var x = function (i) { return L + (W - L - R) * (pts.length === 1 ? 0 : i / (pts.length - 1)); };
    var y = function (val) { return T + (H - T - B) * (1 - val / 100); };

    var out = '';
    [0, 25, 50, 75, 100].forEach(function (g) {
      out += '<line class="gl" x1="' + L + '" y1="' + y(g) + '" x2="' + (W - R) + '" y2="' + y(g) + '"/>';
      out += '<text x="' + (L - 8) + '" y="' + (y(g) + 4) + '" text-anchor="end">' + g + '</text>';
    });
    out += '<line class="ax" x1="' + L + '" y1="' + y(0) + '" x2="' + (W - R) + '" y2="' + y(0) + '"/>';

    var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + x(i) + ' ' + y(p.avg); }).join(' ');
    out += '<path class="ln" d="' + d + '"/>';

    var step = Math.max(1, Math.ceil(pts.length / 8));
    pts.forEach(function (p, i) {
      if (i % step === 0 || i === pts.length - 1) {
        out += '<text x="' + x(i) + '" y="' + (H - 8) + '" text-anchor="middle">' +
               p.date.slice(5).replace('-', '/') + '</text>';
      }
    });
    // The endpoint is direct-labelled; the rest are carried by the axis and tooltip.
    var lastPt = pts[pts.length - 1];
    out += '<circle class="dot" cx="' + x(pts.length - 1) + '" cy="' + y(lastPt.avg) + '" r="4.5"/>';
    out += '<text x="' + (x(pts.length - 1) - 6) + '" y="' + (y(lastPt.avg) - 10) +
           '" text-anchor="end" style="fill:var(--ink);font-size:12px">' + fmt(lastPt.avg) + '</text>';
    svg.innerHTML = out;

    // Generous hit bands, so the target is the column, not the 9px dot.
    var bw = (W - L - R) / pts.length;
    pts.forEach(function (p, i) {
      var hit = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      hit.setAttribute('class', 'hit');
      hit.setAttribute('x', String(x(i) - bw / 2));
      hit.setAttribute('y', String(T));
      hit.setAttribute('width', String(bw));
      hit.setAttribute('height', String(H - T - B));
      hoverable(hit, '<b>' + p.date + '</b><br>Team average ' + fmt(p.avg) + ' / 100');
      svg.appendChild(hit);
    });
  }

  function seqVar(total) {
    var i = Math.min(SEQ.length - 1, Math.max(0, Math.floor(total / 100 * SEQ.length)));
    return 'var(' + SEQ[i] + ')';
  }

  function renderHeat(v) {
    var t = $('heat');
    if (!v.devs.length) { t.innerHTML = ''; $('scale').innerHTML = ''; return; }
    var head = '<thead><tr><th></th>' + v.dates.map(function (d) {
      return '<th>' + d.slice(8) + '</th>';
    }).join('') + '</tr></thead>';

    var body = v.devs.map(function (dev) {
      var cells = v.dates.map(function (date) {
        var r = v.rows.find(function (x) { return x.developer === dev && x.date === date; });
        if (!r || !scored(r)) {
          return '<td data-empty="' + esc(dev + '|' + date + '|' +
            (r ? (r.reason || r.attendance || 'not scored') : 'no record')) + '"></td>';
        }
        return '<td class="v" style="background:' + seqVar(r.total) +
               '" data-tip="' + esc(dev + '|' + date + '|' + fmt(r.total) + '|' +
               fmt(r.process) + '|' + fmt(r.delivery) + '|' + r.done + '/' + r.picked) + '"></td>';
      }).join('');
      return '<tr><th>' + esc(dev) + '</th>' + cells + '</tr>';
    }).join('');
    t.innerHTML = head + '<tbody>' + body + '</tbody>';

    t.querySelectorAll('td.v').forEach(function (td) {
      var p = td.getAttribute('data-tip').split('|');
      hoverable(td, '<b>' + esc(p[0]) + '</b> · ' + p[1] + '<br>Total ' + p[2] +
        ' / 100<br>Process ' + p[3] + ' · Delivery ' + p[4] + '<br>Tasks done ' + p[5]);
    });
    t.querySelectorAll('td[data-empty]').forEach(function (td) {
      var p = td.getAttribute('data-empty').split('|');
      hoverable(td, '<b>' + esc(p[0]) + '</b> · ' + p[1] + '<br>Not scored — ' + esc(p[2]));
    });

    $('scale').innerHTML = '<span>0</span>' + SEQ.map(function (s) {
      return '<i style="background:var(' + s + ')"></i>';
    }).join('') + '<span>100</span><span style="margin-left:10px">' +
      '<i style="background:var(--grid)"></i> not scored</span>';
  }

  function renderTable(v) {
    var t = $('tbl');
    var head = '<thead><tr><th>Developer</th><th>Days</th><th>Process</th><th>Delivery</th>' +
               '<th>Average</th><th>Band</th><th>Tasks done</th></tr></thead>';
    var rows = v.devs.map(function (dev) {
      var mine = v.rows.filter(function (r) { return r.developer === dev; });
      var s = mine.filter(scored);
      var avg = average(mine), band = bandOf(avg);
      var picked = s.reduce(function (a, r) { return a + (r.picked || 0); }, 0);
      var done = s.reduce(function (a, r) { return a + (r.done || 0); }, 0);
      return '<tr><td>' + esc(dev) + '</td>' +
        '<td class="num">' + s.length + '</td>' +
        '<td class="num">' + fmt(averageOf(mine, 'process')) + '</td>' +
        '<td class="num">' + fmt(averageOf(mine, 'delivery')) + '</td>' +
        '<td class="num">' + fmt(avg) + '</td>' +
        '<td>' + (band ? '<i class="dot" style="background:' + bandColor(band) + '"></i>' + band : '<span class="muted">—</span>') + '</td>' +
        '<td class="num">' + done + ' / ' + picked + '</td></tr>';
    }).join('');
    t.innerHTML = head + '<tbody>' + (rows || '<tr><td class="muted">No data.</td></tr>') + '</tbody>';

    var u = $('unscored');
    var miss = v.rows.filter(function (r) { return r.status !== 'Scored'; })
                     .sort(function (a, b) { return a.date < b.date ? 1 : -1; });
    u.innerHTML = '<thead><tr><th>Date</th><th>Developer</th><th>Attendance</th><th>Reason</th></tr></thead><tbody>' +
      (miss.length ? miss.map(function (r) {
        return '<tr><td>' + r.date + '</td><td>' + esc(r.developer) + '</td><td>' +
          esc(r.attendance || '—') + '</td><td class="muted">' + esc(r.reason || '—') + '</td></tr>';
      }).join('') : '<tr><td class="muted">None — every day in this period was scored.</td></tr>') + '</tbody>';
  }

  function draw() {
    var v = slice();
    renderHead(v); renderKpis(v); renderBars(v);
    renderTrend(v); renderHeat(v); renderTable(v);
  }

  document.querySelectorAll('.chip').forEach(function (b) {
    b.addEventListener('click', function () {
      document.querySelectorAll('.chip').forEach(function (o) { o.setAttribute('aria-pressed', 'false'); });
      b.setAttribute('aria-pressed', 'true');
      days = parseInt(b.getAttribute('data-days'), 10);
      draw();
    });
  });
  draw();
  window.addEventListener('scroll', hideTip, { passive: true });
})();
</script>
"""
