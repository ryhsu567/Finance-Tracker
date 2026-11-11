#!/usr/bin/env python3
"""Build the static, multi-page, interactive dashboard from data/budget.db +
config/*.yml.

    python scripts/build_dashboard.py

No metrics-layer module, no tests — calculations live inline here (decision:
move fast, revisit if this file gets unwieldy).

Month toggle and budget-target editing happen client-side in site/app.js
against an embedded JSON payload (window.DATA) — there's no backend, so an
edited target is a browser-local override (localStorage), not a rewrite of
config/budgets.yml. The "pool over time" history chart uses the config
defaults only (not override-aware) — a retrospective view, not the
interactive one.

Emits:
    site/index.html, expenses.html, income.html, investments.html,
    budget.html, import.html   (generated — edit this script, not these)

site/style.css and site/app.js are hand-maintained source files, NOT emitted
by this script; edit them directly.

Open site/index.html directly in a browser.
"""
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "budget.db"
WARNINGS_PATH = ROOT / "data" / "load_warnings.json"
SITE_DIR = ROOT / "site"

CATEGORY_LABELS = {
    "rent": "Rent", "groceries": "Groceries", "utilities": "Utilities",
    "transportation": "Transportation", "eating_out": "Eating out",
    "sweet_treat": "Sweet treat", "entertainment": "Entertainment", "gym": "Gym",
    "shopping": "Shopping", "other": "Other", "travel": "Travel",
}
INCOME_LABELS = {
    "salary": "Salary", "gift": "Gift", "side_hustle": "Side hustle",
    "tax_refund": "Tax refund", "reimbursement": "Reimbursement", "resale": "Resale",
}
TRANSFER_LABELS = {"stock_sale": "Stock sale", "deposit_return": "Deposit return"}
INVESTMENT_LABELS = {"brokerage": "Brokerage", "roth_ira": "Roth IRA",
                     "emergency_fund": "Emergency fund", "crypto": "Crypto"}
# Fixed hue order, not cycled, and an account keeps its hue on every chart
# it appears in — the three series tokens the rest of the site already
# ships, no new colors invented for this page.
# Slots 1/4/3/2 of the reference palette, in this order: the dict order is the
# stack order, and the validator checks adjacent pairs. Yellow next to orange
# fails outright (CVD ΔE 4.8 dark), so Roth IRA sits between blue and aqua.
INVESTMENT_COLORS = {"brokerage": "var(--series-1)", "roth_ira": "var(--series-4)",
                     "emergency_fund": "var(--series-3)", "crypto": "var(--series-2)"}

STATUS_HEX = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b", "neutral": "#898781"}
STATUS_TEXT = {"good": "on pace", "warning": "watch", "critical": "over pace", "neutral": "—"}

NAV = [("index.html", "Dashboard"), ("expenses.html", "Expenses"),
       ("income.html", "Income"), ("investments.html", "Investments"),
       ("budget.html", "Budget"), ("import.html", "Import")]


# ---------------------------------------------------------------- data ----

def load_accounts():
    """config/accounts.yml — hand-entered balance snapshots. Optional: a
    missing file just means no balances are recorded, not a broken build."""
    path = ROOT / "config" / "accounts.yml"
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text()) or {}).get("accounts") or {}


def load_data():
    categories_cfg = yaml.safe_load((ROOT / "config" / "categories.yml").read_text())
    budgets_cfg = yaml.safe_load((ROOT / "config" / "budgets.yml").read_text())
    targets = {t["category"]: float(t["monthly_target"]) for t in budgets_cfg["targets"]}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    txns = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY date")]
    conn.close()
    return categories_cfg, targets, txns, load_accounts()


def load_warnings():
    """Loader integrity-check warnings (SPEC §6.2/§6.3) - written by
    scripts/load.py every run. Surfaced on the Dashboard so a real data
    problem doesn't sit invisible in a terminal nobody's looking at."""
    if not WARNINGS_PATH.exists():
        return []
    try:
        return json.loads(WARNINGS_PATH.read_text()).get("warnings", [])
    except (json.JSONDecodeError, OSError):
        return []


def month_key(iso_date):
    return iso_date[:7]

def label_for(cat):
    return (CATEGORY_LABELS.get(cat) or INCOME_LABELS.get(cat)
            or TRANSFER_LABELS.get(cat) or INVESTMENT_LABELS.get(cat) or cat)

def money(x):
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"

def money_compact(x):
    """Axis-label money: $12k, not $12,236. Exact figures stay on the direct
    labels and in tooltips, where precision is actually read."""
    if abs(x) >= 1000:
        thousands = x / 1000
        return f"${thousands:,.0f}k" if abs(x) % 1000 < 1 else f"${thousands:,.1f}k"
    return f"${x:,.0f}"


def nice_step(raw):
    """Round a raw tick interval up to a human one (1/2/3/5 x 10^n) so the
    axis reads $0 / $3k / $6k / $9k / $12k instead of $0 / $3,059 / $6,118."""
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 3, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"

def month_label(m):
    y, mo = m.split("-")
    return date(int(y), int(mo), 1).strftime("%b %Y")

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def and_list(items):
    """'travel' / 'travel and shopping' / 'travel, shopping and gym' - so a
    caption that names a derived set reads as a sentence however many
    categories happen to be in it."""
    items = [esc(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ------------------------------------------------------------- metrics ----

def compute(categories_cfg, targets, txns, accounts_cfg=None):
    expense_cats = categories_cfg["expense"]
    cadence_of, group_of = {}, {}
    baseline_order, discretionary_order = [], []
    for cats in expense_cats.values():
        for cat, meta in cats.items():
            cadence_of[cat] = meta["cadence"]
            group_of[cat] = meta["budget_group"]
            (baseline_order if meta["budget_group"] == "baseline" else discretionary_order).append(cat)

    months = sorted({month_key(t["date"]) for t in txns})
    latest_month = months[-1]
    latest_date = max(date.fromisoformat(t["date"]) for t in txns if month_key(t["date"]) == latest_month)

    def days_in_month(year, month):
        return (date(year + (month == 12), month % 12 + 1, 1) - date(year, month, 1)).days

    # Pace is measured against the real calendar date, not the newest
    # transaction we happen to have loaded - otherwise staging one late
    # transaction (an income row entered a few days after it landed, say)
    # silently drags "day of month" along with it, understating how much
    # of the month's expense data is actually still missing. If the latest
    # month in the data isn't the actual current month (nothing loaded yet
    # for this month), treat that last month as fully elapsed (pace 1.0)
    # rather than pretending it's still in progress.
    today = date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"
    if latest_month == current_month:
        day_of_month = today.day
        days_in_latest = days_in_month(today.year, today.month)
    else:
        y, mo = (int(p) for p in latest_month.split("-"))
        days_in_latest = days_in_month(y, mo)
        day_of_month = days_in_latest
    live_pace = day_of_month / days_in_latest

    by_month = {m: dict(income=0.0, expense=0.0, investment=0.0,
                         expenseByCat=defaultdict(float),
                         incomeByCat=defaultdict(float), transferByCat=defaultdict(float),
                         investmentByCat=defaultdict(float),
                         expenseTxns=[], incomeTxns=[], investmentTxns=[])
                for m in months}

    for t in txns:
        m = month_key(t["date"])
        row = by_month[m]
        txn_out = dict(date=t["date"], description=t["description"], category=t["category"], amount=t["amount"])
        if t["kind"] == "expense":
            row["expense"] += t["amount"]
            row["expenseByCat"][t["category"]] += t["amount"]
            row["expenseTxns"].append(txn_out)
        elif t["kind"] == "income":
            row["income"] += t["amount"]
            row["incomeByCat"][t["category"]] += t["amount"]
            row["incomeTxns"].append(dict(txn_out, kind="income"))
        elif t["kind"] == "transfer":
            row["transferByCat"][t["category"]] += t["amount"]
            row["incomeTxns"].append(dict(txn_out, kind="transfer"))
        elif t["kind"] == "investment":
            # Never folded into expense: a contribution doesn't reduce net
            # worth, it moves cash between accounts (SPEC §2/§5).
            row["investment"] += t["amount"]
            row["investmentByCat"][t["category"]] += t["amount"]
            row["investmentTxns"].append(txn_out)
        else:
            # Every kind in load.py's VALID_KINDS needs a branch here.
            # Falling through silently would drop the row from every view on
            # the site — SPEC §6.1's failure mode, one layer downstream.
            raise SystemExit(
                f"build_dashboard: unhandled kind {t['kind']!r} "
                f"({t['date']} {t['description']!r}) — add a branch in compute()."
            )

    for m in months:
        by_month[m]["expenseTxns"].sort(key=lambda t: t["date"], reverse=True)
        by_month[m]["incomeTxns"].sort(key=lambda t: t["date"], reverse=True)
        by_month[m]["investmentTxns"].sort(key=lambda t: t["date"], reverse=True)
        # plain dicts for JSON (defaultdict repr is fine but keep it tidy)
        by_month[m]["expenseByCat"] = dict(by_month[m]["expenseByCat"])
        by_month[m]["incomeByCat"] = dict(by_month[m]["incomeByCat"])
        by_month[m]["transferByCat"] = dict(by_month[m]["transferByCat"])
        by_month[m]["investmentByCat"] = dict(by_month[m]["investmentByCat"])

    n_months = len(months)

    # --- all-time aggregates (static views, not toggle/override-aware) ---
    all_time_cat = defaultdict(float)
    all_time_income_cat = defaultdict(float)
    all_time_transfer_cat = defaultdict(float)
    all_time_investment_cat = defaultdict(float)
    for m in months:
        for cat, v in by_month[m]["expenseByCat"].items():
            all_time_cat[cat] += v
        for cat, v in by_month[m]["incomeByCat"].items():
            all_time_income_cat[cat] += v
        for cat, v in by_month[m]["transferByCat"].items():
            all_time_transfer_cat[cat] += v
        for cat, v in by_month[m]["investmentByCat"].items():
            all_time_investment_cat[cat] += v

    # SPEC §5's `underlying_spend`, implemented at last. The spec defined it
    # as "expenses excluding all tagged transactions — the trend line that
    # actually means something", but tagging was descoped (§11), so the
    # definition lost its basis and the metric was never built. Cadence
    # carries the same meaning on an axis that still exists: a `lumpy`
    # category has no monthly rhythm (saved up over months, then spent in
    # one go), so excluding it leaves a line a trend can be read off. One
    # $950 flight booking otherwise moves the total more than a whole
    # month's groceries. Derived per month, never stored. The list is empty
    # when no category is lumpy, and the chart falls back to one line.
    lumpy_cats = sorted(c for c, cad in cadence_of.items() if cad == "lumpy")

    def underlying_for(m):
        lump = sum(by_month[m]["expenseByCat"].get(c, 0.0) for c in lumpy_cats)
        return by_month[m]["expense"] - lump

    # A `lumpy` category is a sinking fund, not a monthly pace (user
    # decision 2026-11-11): the target accrues every elapsed month and
    # spending draws the balance down, so the figure worth reading is the
    # bucket level rather than a ratio. `ytd vs target * nMonths` was
    # already this arithmetic - it just rendered as a percentage, which can
    # only ever say "over" or "under" and so cannot express saving up and
    # then spending. Accrual starts at the first month in the ledger (the
    # decision was to carry all of 2026, not to open a fresh bucket), and
    # the balance may go negative: travel ran $4,923.75 against a $150/mo
    # accrual, and flooring at zero would hide exactly that. Derived here,
    # never stored (CLAUDE.md), and mirrored in app.js's categoryResult().
    def fund_for(cat):
        rate, cum, out = targets.get(cat, 0) or 0, 0.0, []
        for i, m in enumerate(months, start=1):
            cum += by_month[m]["expenseByCat"].get(cat, 0.0)
            out.append(dict(month=m, accrued=rate * i, spent=cum, balance=rate * i - cum))
        return out

    lumpy_funds = {c: fund_for(c) for c in lumpy_cats}

    monthly_series = [dict(month=m, income=by_month[m]["income"], expense=by_month[m]["expense"],
                            underlying=underlying_for(m),
                            investment=by_month[m]["investment"]) for m in months]

    # SPEC §5: contributions are judged against net cash flow, not against
    # expenses — a month that invests more than it netted is drawing down
    # cash rather than saving out of income, which the source sheets could
    # not show at all. Derived per month, never stored.
    investment_months = [
        dict(month=m,
             contributed=by_month[m]["investment"],
             net=by_month[m]["income"] - by_month[m]["expense"],
             drawdown=(by_month[m]["investment"] > 0
                       and by_month[m]["investment"] > by_month[m]["income"] - by_month[m]["expense"]))
        for m in months
    ]
    # Per account: what's been contributed, plus the optional market-value
    # snapshot from config. Nothing is withdrawn from these accounts, so the
    # contributed total is the account total; the balance is the one figure
    # the ledger can't produce (market value isn't a transaction), so it's
    # carried with its as_of date and never inferred from the contributions.
    accounts_cfg = accounts_cfg or {}
    account_rollup = []
    for cat in INVESTMENT_LABELS:
        cfg = accounts_cfg.get(cat) or {}
        contributed = all_time_investment_cat.get(cat, 0.0)
        balance = cfg.get("balance")
        if contributed == 0 and balance in (None, ""):
            continue      # an account with neither cash in nor a balance isn't in use
        account_rollup.append(dict(
            key=cat, label=label_for(cat), contributed=contributed,
            balance=(float(balance) if balance not in (None, "") else None),
            as_of=cfg.get("as_of"),
        ))

    funded_months = [r for r in investment_months if r["contributed"] > 0]

    # Lumpy-cadence categories are excluded from the paced pool (SPEC §5:
    # "a trip is one decision, not a monthly overrun") - matches app.js's
    # poolResult. A single early-month lumpy purchase shouldn't read as the
    # whole pool being over pace; lumpy gets its own sinking-fund balance.
    pooled_discretionary = [c for c in discretionary_order if cadence_of[c] != "lumpy"]
    lumpy_discretionary = [c for c in discretionary_order if cadence_of[c] == "lumpy"]
    # Displayed highest-budget-first on the Budget page (user preference).
    pooled_discretionary.sort(key=lambda c: -targets.get(c, 0))
    lumpy_discretionary.sort(key=lambda c: -targets.get(c, 0))

    def pool_for_month(m, pace_):
        target_m = sum(targets.get(c, 0) for c in pooled_discretionary)
        actual_m = sum(by_month[m]["expenseByCat"].get(c, 0.0) for c in pooled_discretionary)
        expected = target_m * pace_
        ratio = actual_m / expected if expected else None
        return dict(month=m, target=target_m, actual=actual_m, expected=expected,
                    ratio=ratio, status=status_for_ratio(ratio))

    monthly_pool_history = [pool_for_month(m, 1.0 if m != latest_month else live_pace) for m in months]

    # --- client-side payload ---
    categories_json = {
        cat: dict(label=label_for(cat), cadence=cadence_of[cat], group=group_of[cat],
                  baseTarget=targets.get(cat))
        for cat in cadence_of
    }
    investment_categories_json = {cat: dict(label=label_for(cat), color=INVESTMENT_COLORS[cat])
                                   for cat in INVESTMENT_LABELS}
    income_categories_json = {cat: dict(label=label_for(cat), isTransfer=False) for cat in INCOME_LABELS}
    income_categories_json.update({cat: dict(label=label_for(cat), isTransfer=True) for cat in TRANSFER_LABELS})

    client_data = dict(
        months=months,
        monthLabels={m: month_label(m) for m in months},
        livePace=live_pace,
        dayOfMonth=day_of_month,
        categories=categories_json,
        incomeCategories=income_categories_json,
        investmentCategories=investment_categories_json,
        byMonth=by_month,
        order=dict(baseline=baseline_order, discretionary=discretionary_order),
    )

    return dict(
        months=months, monthly_series=monthly_series, lumpy_cats=lumpy_cats,
        lumpy_funds=lumpy_funds,
        all_time_expense_ranked=sorted(all_time_cat.items(), key=lambda kv: -kv[1]),
        all_time_income_ranked=sorted(all_time_income_cat.items(), key=lambda kv: -kv[1]),
        all_time_transfer_ranked=sorted(all_time_transfer_cat.items(), key=lambda kv: -kv[1]),
        all_time_investment_ranked=sorted(all_time_investment_cat.items(), key=lambda kv: -kv[1]),
        total_investment_all_time=sum(all_time_investment_cat.values()),
        investment_months=investment_months,
        has_investments=bool(all_time_investment_cat),
        account_rollup=account_rollup,
        monthly_pool_history=monthly_pool_history,
        live_pace=live_pace, day_of_month=day_of_month, n_months=n_months, latest_date=latest_date,
        total_income_all_time=sum(all_time_income_cat.values()),
        total_expense_all_time=sum(all_time_cat.values()),
        client_data=client_data,
        latest_month=latest_month,
        baseline_order=baseline_order, discretionary_order=discretionary_order,
        pooled_discretionary=pooled_discretionary, lumpy_discretionary=lumpy_discretionary,
        cadence_of=cadence_of, group_of=group_of, targets=targets,
    )


def status_for_ratio(ratio):
    if ratio is None:
        return "neutral"
    if ratio <= 1.0:
        return "good"
    if ratio <= 1.2:
        return "warning"
    return "critical"


# --------------------------------------------------------------- charts ---

def render_line_svg(chart_id, month_labels, series, y_fmt, tooltip_fmt):
    W, H = 760, 260
    # PAD_R leaves room for the end-of-line value label (e.g. "$2,847") to
    # the right of the last point - it was clipped by the viewBox edge at 20px.
    PAD_L, PAD_R, PAD_T, PAD_B = 52, 60, 16, 28
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    n = len(month_labels)
    # Domain includes 0 even if every value is positive (old behavior) but
    # also extends below 0 if a series actually dips negative (e.g. net
    # cash flow) - a plain max()*1.15 silently mis-plots negative values.
    all_vals = [v for s in series for v in s["values"]] or [0]
    raw_lo, raw_hi = min(0, min(all_vals)), max(0, max(all_vals))
    span = (raw_hi - raw_lo) or 1
    y_hi = raw_hi + span * 0.15
    y_lo = raw_lo - (span * 0.15 if raw_lo < 0 else 0)

    def x_at(i):
        return PAD_L + (plot_w * i / max(1, n - 1)) if n > 1 else PAD_L + plot_w / 2

    def y_at(v):
        return PAD_T + plot_h - (v - y_lo) / (y_hi - y_lo) * plot_h

    grid = []
    for step in range(5):
        v = y_lo + (y_hi - y_lo) * step / 4
        y = y_at(v)
        grid.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" stroke="var(--gridline)" stroke-width="1"/>'
                     f'<text x="{PAD_L-8}" y="{y+4:.1f}" text-anchor="end" class="axis-label">{esc(y_fmt(v))}</text>')
    if y_lo < 0 < y_hi:
        zy = y_at(0)
        grid.append(f'<line x1="{PAD_L}" y1="{zy:.1f}" x2="{W-PAD_R}" y2="{zy:.1f}" stroke="var(--baseline)" stroke-width="1.5"/>')

    lines, dots = [], []
    end_points = []  # (y, x, color, text) - collected then de-collided together
    for s in series:
        pts = [(x_at(i), y_at(v)) for i, v in enumerate(s["values"])]
        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        dash = ' stroke-dasharray="5 4"' if s.get("dash") else ""
        lines.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" stroke-width="2"{dash} '
                      f'stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in pts:
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{s["color"]}" '
                         f'stroke="var(--surface-1)" stroke-width="2"/>')
        if pts:
            lx, ly = pts[-1]
            end_points.append([ly, lx, s["color"], tooltip_fmt(s["values"][-1])])

    # Nudge apart end labels that would otherwise overlap (marks-and-anatomy.md:
    # "when end-labels collide, don't stack them" - min separation, not a fixed offset).
    MIN_GAP = 13
    end_points.sort(key=lambda p: p[0])
    for i in range(1, len(end_points)):
        if end_points[i][0] - end_points[i - 1][0] < MIN_GAP:
            end_points[i][0] = end_points[i - 1][0] + MIN_GAP
    end_labels = [f'<text x="{lx+6:.1f}" y="{ly+4:.1f}" class="end-label" fill="{color}">{esc(text)}</text>'
                  for ly, lx, color, text in end_points]

    x_labels = [f'<text x="{x_at(i):.1f}" y="{H-8}" text-anchor="middle" class="axis-label">{esc(m[:3])}</text>'
                for i, m in enumerate(month_labels)]

    hit_cols = []
    col_w = plot_w / n if n else plot_w
    for i, m in enumerate(month_labels):
        x = x_at(i)
        rows = [[s["name"], tooltip_fmt(s["values"][i]), s["color"]] for s in series]
        rows_json = esc(json.dumps(rows))
        hit_cols.append(
            f'<rect class="hit-target" data-crosshair="crosshair-{chart_id}" data-cx="{x:.1f}" '
            f'data-tooltip-title="{esc(m)}" data-tooltip-rows="{rows_json}" data-month-idx="{i}" '
            f'x="{x-col_w/2:.1f}" y="{PAD_T}" width="{col_w:.1f}" height="{plot_h}" fill="transparent"/>'
        )

    return f"""
  <svg viewBox="0 0 {W} {H}" class="chart-svg">
    {''.join(grid)}
    {''.join(lines)}
    {''.join(dots)}
    {''.join(x_labels)}
    {''.join(end_labels)}
    <line id="crosshair-{chart_id}" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T+plot_h}"
          stroke="var(--baseline)" stroke-width="1" style="display:none"/>
    {''.join(hit_cols)}
  </svg>"""


def render_sparkline(values, color, w=96, h=28):
    """Trailing trend for a stat tile - context, not the toggle-scoped value.
    De-emphasized (thin, muted) except the current point, per marks-and-anatomy's
    stat-tile contract: 'trend, de-emphasis hue, current period in the accent.'"""
    if len(values) < 2 or all(v == 0 for v in values):
        return ""
    pad = 3
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    n = len(values)

    def x_at(i):
        return pad + (w - 2 * pad) * i / (n - 1)

    def y_at(v):
        return h - pad - (v - lo) / rng * (h - 2 * pad)

    pts = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    lx, ly = pts[-1]
    return (f'<svg viewBox="0 0 {w} {h}" class="sparkline" width="{w}" height="{h}">'
            f'<path d="{path}" fill="none" stroke="var(--text-muted)" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="0.55"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.5" fill="{color}"/>'
            f'</svg>')


def render_trend_with_gap_svg(chart_id, month_labels, income_vals, expense_vals):
    """Income vs expense lines, with the gap between them shaded per-segment
    - green where income is ahead (surplus), red where expenses caught up
    or passed it (deficit) - plus net cash flow itself as a third, dashed
    line, so the gap is both visible and readable as an exact number."""
    W, H = 760, 260
    PAD_L, PAD_R, PAD_T, PAD_B = 52, 60, 16, 28
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    n = len(month_labels)
    net_vals = [inc - exp for inc, exp in zip(income_vals, expense_vals)]
    hi = max(income_vals + expense_vals, default=1) or 1
    y_hi = hi * 1.15
    y_lo = min([0] + net_vals)
    if y_lo < 0:
        y_lo -= hi * 0.05

    def x_at(i):
        return PAD_L + (plot_w * i / max(1, n - 1)) if n > 1 else PAD_L + plot_w / 2

    def y_at(v):
        return PAD_T + plot_h - (v - y_lo) / (y_hi - y_lo) * plot_h

    grid = []
    for step in range(5):
        v = y_lo + (y_hi - y_lo) * step / 4
        y = y_at(v)
        grid.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" stroke="var(--gridline)" stroke-width="1"/>'
                     f'<text x="{PAD_L-8}" y="{y+4:.1f}" text-anchor="end" class="axis-label">{v/1000:.1f}k</text>')
    if y_lo < 0 < y_hi:
        zy = y_at(0)
        grid.append(f'<line x1="{PAD_L}" y1="{zy:.1f}" x2="{W-PAD_R}" y2="{zy:.1f}" stroke="var(--baseline)" stroke-width="1.5"/>')

    income_pts = [(x_at(i), y_at(v)) for i, v in enumerate(income_vals)]
    expense_pts = [(x_at(i), y_at(v)) for i, v in enumerate(expense_vals)]
    net_pts = [(x_at(i), y_at(v)) for i, v in enumerate(net_vals)]

    fills = []
    for i in range(n - 1):
        positive = (income_vals[i] + income_vals[i + 1]) >= (expense_vals[i] + expense_vals[i + 1])
        color = "var(--good)" if positive else "var(--critical)"
        x1, y1i = income_pts[i]; x2, y2i = income_pts[i + 1]
        _, y1e = expense_pts[i]; _, y2e = expense_pts[i + 1]
        fills.append(f'<polygon points="{x1:.1f},{y1i:.1f} {x2:.1f},{y2i:.1f} {x2:.1f},{y2e:.1f} {x1:.1f},{y1e:.1f}" '
                      f'fill="{color}" opacity="0.14"/>')

    income_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in income_pts)
    expense_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in expense_pts)
    net_path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in net_pts)
    lines = [
        f'<path d="{income_path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
        f'<path d="{expense_path}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
        f'<path d="{net_path}" fill="none" stroke="var(--text-secondary)" stroke-width="2" stroke-dasharray="5 4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>',
    ]
    dots = [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2"/>' for x, y in income_pts]
    dots += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--series-2)" stroke="var(--surface-1)" stroke-width="2"/>' for x, y in expense_pts]
    dots += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--text-secondary)" stroke="var(--surface-1)" stroke-width="2"/>' for x, y in net_pts]

    end_points = []
    if income_pts:
        lx, ly = income_pts[-1]
        end_points.append([ly, lx, "var(--series-1)", money(income_vals[-1])])
        lx, ly = expense_pts[-1]
        end_points.append([ly, lx, "var(--series-2)", money(expense_vals[-1])])
        lx, ly = net_pts[-1]
        end_points.append([ly, lx, "var(--text-secondary)", "net " + money(net_vals[-1])])
    MIN_GAP = 13
    end_points.sort(key=lambda p: p[0])
    for i in range(1, len(end_points)):
        if end_points[i][0] - end_points[i - 1][0] < MIN_GAP:
            end_points[i][0] = end_points[i - 1][0] + MIN_GAP
    end_labels = [f'<text x="{lx+6:.1f}" y="{ly+4:.1f}" class="end-label" fill="{color}">{esc(text)}</text>'
                  for ly, lx, color, text in end_points]

    x_labels = [f'<text x="{x_at(i):.1f}" y="{H-8}" text-anchor="middle" class="axis-label">{esc(m[:3])}</text>'
                for i, m in enumerate(month_labels)]

    hit_cols = []
    col_w = plot_w / n if n else plot_w
    for i, m in enumerate(month_labels):
        x = x_at(i)
        net = income_vals[i] - expense_vals[i]
        net_color = "var(--good)" if net >= 0 else "var(--critical)"
        rows = [["Income", money(income_vals[i]), "var(--series-1)"],
                ["Expenses", money(expense_vals[i]), "var(--series-2)"],
                ["Net", money(net), net_color]]
        rows_json = esc(json.dumps(rows))
        hit_cols.append(f'<rect class="hit-target" data-crosshair="crosshair-{chart_id}" data-cx="{x:.1f}" '
                         f'data-tooltip-title="{esc(m)}" data-tooltip-rows="{rows_json}" data-month-idx="{i}" '
                         f'x="{x-col_w/2:.1f}" y="{PAD_T}" width="{col_w:.1f}" height="{plot_h}" fill="transparent"/>')

    return f"""
  <svg viewBox="0 0 {W} {H}" class="chart-svg">
    {''.join(grid)}
    {''.join(fills)}
    {''.join(lines)}
    {''.join(dots)}
    {''.join(x_labels)}
    {''.join(end_labels)}
    <line id="crosshair-{chart_id}" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T+plot_h}"
          stroke="var(--baseline)" stroke-width="1" style="display:none"/>
    {''.join(hit_cols)}
  </svg>"""


def render_bar_chart_svg(ranked, color="var(--series-1)", value_fmt=money):
    if not ranked:
        return '<p class="card-sub">No data.</p>'
    W = 760
    row_h = 28
    H = row_h * len(ranked) + 10
    PAD_L, PAD_R = 130, 64
    plot_w = W - PAD_L - PAD_R
    max_val = max((v for _, v in ranked), default=1) or 1
    bars = []
    for i, (cat, val) in enumerate(ranked):
        y = 10 + i * row_h
        bar_w = (val / max_val) * plot_w
        label = label_for(cat)
        rows_json = esc(json.dumps([[label, value_fmt(val), None]]))
        bars.append(f"""
      <text x="{PAD_L-8}" y="{y+15}" text-anchor="end" class="axis-label">{esc(label)}</text>
      <rect class="hit-target" data-tooltip-title="{esc(label)}" data-tooltip-rows="{rows_json}"
            x="{PAD_L}" y="{y+3}" width="{bar_w:.1f}" height="18" rx="4" fill="{color}"/>
      <text x="{PAD_L+bar_w+6:.1f}" y="{y+16}" class="axis-label">{esc(value_fmt(val))}</text>""")
    return f'<svg viewBox="0 0 {W} {H}" class="chart-svg">{"".join(bars)}</svg>'


def render_legend(items, dashed_last=False):
    """items: [(label, color), ...]. A legend is always present for >= 2
    series, so identity never rests on color alone."""
    keys = []
    for i, (label, color) in enumerate(items):
        dashed = dashed_last and i == len(items) - 1
        swatch = (f'background:{color}; border-top:2px dashed {color}; height:0;'
                  if dashed else f'background:{color}; height:8px; border-radius:2px;')
        keys.append(f'<span class="legend-key"><span class="legend-swatch" style="{swatch}"></span>{esc(label)}</span>')
    return f'<div class="legend">{"".join(keys)}</div>'


def render_stacked_bar_svg(chart_id, month_labels, keys, values_by_month, colors, value_fmt=money):
    """Stacked bars: magnitude per month *and* its composition, which a line
    can't carry. values_by_month is one {key: amount} per month, keys in a
    fixed order so an account keeps its hue and its slot when a month is
    missing it.

    Mark spec: 2px surface gap between segments, 4px rounded top on the
    outermost segment only (the stack is anchored to the baseline), one
    hit target per column rather than per segment — a $40 crypto sliver is
    too thin to hover, and the useful tooltip is the whole month's split."""
    n = len(month_labels)
    totals = [sum(d.get(k, 0.0) for k in keys) for d in values_by_month]
    if not n or not any(totals):
        return '<p class="card-sub">No contributions recorded yet.</p>'

    W, H = 760, 260
    PAD_L, PAD_R, PAD_T, PAD_B = 52, 24, 22, 28
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    GAP, ROUND = 2, 4
    step = nice_step(max(totals) * 1.08 / 4)
    y_hi = step * 4
    col_w = plot_w / n
    bar_w = min(42.0, col_w * 0.62)

    def x_at(i):
        return PAD_L + col_w * (i + 0.5)

    def y_at(v):
        return PAD_T + plot_h - (v / y_hi) * plot_h

    grid = []
    for tick in range(5):
        v = step * tick
        y = y_at(v)
        grid.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" stroke="var(--gridline)" stroke-width="1"/>'
                    f'<text x="{PAD_L-8}" y="{y+4:.1f}" text-anchor="end" class="axis-label">{esc(money_compact(v))}</text>')
    base_y = y_at(0)
    grid.append(f'<line x1="{PAD_L}" y1="{base_y:.1f}" x2="{W-PAD_R}" y2="{base_y:.1f}" stroke="var(--baseline)" stroke-width="1.5"/>')

    # Only the latest month and the biggest month get a direct total label —
    # a number over every bar is noise, the rest is a hover away.
    biggest = max(range(n), key=lambda i: totals[i])
    labelled = {n - 1, biggest}

    segs, labels, hits = [], [], []
    for i, d in enumerate(values_by_month):
        x = x_at(i) - bar_w / 2
        present = [(k, d.get(k, 0.0)) for k in keys if d.get(k, 0.0) > 0]
        cursor = base_y
        for j, (k, v) in enumerate(present):
            h = (v / y_hi) * plot_h
            top = cursor - h
            is_top = j == len(present) - 1
            draw_h = max(1.0, h - (0 if is_top else GAP))
            color = colors[k]
            if is_top:
                r = min(ROUND, draw_h, bar_w / 2)
                segs.append(
                    f'<path d="M {x:.1f} {top+draw_h:.1f} L {x:.1f} {top+r:.1f} '
                    f'Q {x:.1f} {top:.1f} {x+r:.1f} {top:.1f} L {x+bar_w-r:.1f} {top:.1f} '
                    f'Q {x+bar_w:.1f} {top:.1f} {x+bar_w:.1f} {top+r:.1f} '
                    f'L {x+bar_w:.1f} {top+draw_h:.1f} Z" fill="{color}"/>')
            else:
                segs.append(f'<rect x="{x:.1f}" y="{top+GAP:.1f}" width="{bar_w:.1f}" '
                            f'height="{draw_h:.1f}" fill="{color}"/>')
            cursor = top
        if totals[i] > 0 and i in labelled:
            labels.append(f'<text x="{x_at(i):.1f}" y="{cursor-6:.1f}" text-anchor="middle" '
                          f'class="end-label">{esc(value_fmt(totals[i]))}</text>')
        rows = [[label_for(k), value_fmt(d.get(k, 0.0)), colors[k]] for k in keys if d.get(k, 0.0) > 0]
        rows.append(["Total", value_fmt(totals[i]), None] if totals[i] > 0
                     else ["No contributions", "", None])
        hits.append(
            f'<rect class="hit-target" data-tooltip-title="{esc(month_labels[i])}" '
            f'data-tooltip-rows="{esc(json.dumps(rows))}" data-month-idx="{i}" '
            f'x="{x_at(i)-col_w/2:.1f}" y="{PAD_T}" width="{col_w:.1f}" height="{plot_h}" fill="transparent"/>')

    x_labels = [f'<text x="{x_at(i):.1f}" y="{H-8}" text-anchor="middle" class="axis-label">{esc(m[:3])}</text>'
                for i, m in enumerate(month_labels)]

    return f"""
  <svg viewBox="0 0 {W} {H}" class="chart-svg">
    {''.join(grid)}
    {''.join(segs)}
    {''.join(x_labels)}
    {''.join(labels)}
    {''.join(hits)}
  </svg>"""


def render_mix_bar(ranked, colors, value_fmt=money):
    """One 100%-wide stacked bar: a share-of-whole read, where three numbers
    don't justify a pie. Percentages are labelled directly, so the shares are
    readable without measuring the segments."""
    total = sum(v for _, v in ranked)
    if not total:
        return '<p class="card-sub">No contributions recorded yet.</p>'
    W, H = 760, 56
    GAP, ROUND = 2, 4
    x = 0.0
    segs, labels, spillover = [], [], []
    usable = W - GAP * max(0, len(ranked) - 1)
    for i, (cat, val) in enumerate(ranked):
        w = usable * (val / total)
        share = val / total
        rows = [[label_for(cat), f"{value_fmt(val)} · {share*100:.0f}%", colors[cat]]]
        segs.append(
            f'<rect class="hit-target" data-tooltip-title="Mix" data-tooltip-rows="{esc(json.dumps(rows))}" '
            f'x="{x:.1f}" y="0" width="{max(1.0, w):.1f}" height="22" rx="{ROUND}" fill="{colors[cat]}"/>')
        text = f"{label_for(cat)} {share*100:.0f}%"
        # Label the segment only if its own width can hold the text - a share
        # test alone puts a 105px label on a 60px segment, straight into its
        # neighbour. The rest are picked up by the shares line below.
        if w >= len(text) * 6.4 + 8:
            labels.append(f'<text x="{x + w/2:.1f}" y="40" text-anchor="middle" class="axis-label">'
                          f'{esc(text)}</text>')
        else:
            spillover.append(text)
        x += w + GAP
    spill_html = (f'<div class="card-sub" style="margin-top:2px;">{esc(" · ".join(spillover))}</div>'
                  if spillover else "")
    return (f'<svg viewBox="0 0 {W} {H}" class="chart-svg">{"".join(segs)}{"".join(labels)}</svg>'
            + spill_html)


def render_stat_tile(tile_id, label, accent=None, with_delta=False, sparkline_values=None, spark_color=None):
    """Value/sub filled in by JS on load — id'd so app.js can find it."""
    style = f' style="--tile-accent:{accent}"' if accent else ""
    delta_html = f'<div class="tile-delta" id="{tile_id}-delta"></div>' if with_delta else ""
    spark_html = ""
    if sparkline_values:
        svg = render_sparkline(sparkline_values, spark_color or accent or "var(--series-1)")
        if svg:
            spark_html = f'<div class="tile-spark">{svg}</div>'
    return (f'<div class="tile"{style}><div class="tile-head-row"><div class="tile-label">{esc(label)}</div>{spark_html}</div>'
            f'<div class="tile-value" id="{tile_id}-value">—</div>'
            f'<div class="tile-sub" id="{tile_id}-sub"></div>{delta_html}</div>')


def render_stat_tile_static(label, value, accent=None, sub=None):
    """A stat tile whose value is fixed at build time (e.g. YTD totals) —
    not toggle-dependent, so no JS-filled id like render_stat_tile's."""
    style = f' style="--tile-accent:{accent}"' if accent else ""
    sub_html = f'<div class="tile-sub">{esc(sub)}</div>' if sub else ""
    return (f'<div class="tile"{style}><div class="tile-label">{esc(label)}</div>'
            f'<div class="tile-value">{esc(value)}</div>{sub_html}</div>')


def render_meter_skeleton(cat, label, cadence, editable=True):
    input_html = (f'<span class="target-input-wrap"><span class="prefix">$</span>'
                  f'<input type="number" class="target-input" id="input-{cat}" data-cat="{esc(cat)}" step="1" min="0"></span>'
                  if editable else '<span class="meter-target" id="target-{cat}"></span>')
    return f"""
    <div class="meter-row" id="meter-{cat}" data-cat="{esc(cat)}">
      <div class="meter-head">
        <span class="meter-label">{esc(label)}</span>
        <span class="meter-cadence">{esc(cadence)}</span>
        <span class="status-badge neutral m-badge"></span>
        {input_html}
      </div>
      <div class="meter-track m-track"><div class="meter-fill m-fill"></div></div>
      <div class="meter-note m-note"></div>
    </div>"""


def render_pool_skeleton(dom_id, label):
    return f"""
    <div class="pool-card" id="{dom_id}">
      <div class="meter-head">
        <span class="meter-label">{esc(label)}</span>
        <span class="status-badge neutral m-badge"></span>
        <span class="meter-target">target <span class="m-target-val">—</span>/mo</span>
      </div>
      <div class="meter-track m-track" style="height:10px;"><div class="meter-fill m-fill"></div></div>
      <div class="meter-note m-note"></div>
    </div>"""


def render_txn_table_static(txns, amount_label="Amount"):
    if not txns:
        return '<p class="card-sub">No transactions.</p>'
    rows = "".join(f"""<tr>
      <td>{esc(t['date'])}</td><td>{esc(t['description'])}</td>
      <td><span class="cat-tag">{esc(label_for(t['category']))}</span></td>
      <td class="num">{esc(money(t['amount']))}</td>
    </tr>""" for t in txns)
    return f"""<table class="data-table">
      <thead><tr><th>Date</th><th>Description</th><th>Category</th><th class="num">{esc(amount_label)}</th></tr></thead>
      <tbody>{rows}</tbody></table>"""


# ---------------------------------------------------------------- shell ---

def render_shell(title, active_file, body_html, client_data, init_js):
    nav_links = "".join(
        f'<a href="{href}"{" class=\"active\"" if href == active_file else ""}>{label}</a>'
        for href, label in NAV
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Budget</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<nav class="topnav">
  <span class="brand">Budget</span>
  {nav_links}
</nav>
<div class="wrap">
{body_html}
</div>
<div id="tooltip"></div>
<script src="app.js"></script>
<script>
window.DATA = {json.dumps(client_data)};
{init_js}
</script>
</body>
</html>"""


# --------------------------------------------------------- page: dashboard

def page_dashboard(d, warnings):
    months_lbl = [month_label(m["month"]) for m in d["monthly_series"]]
    income_vals = [m["income"] for m in d["monthly_series"]]
    expense_vals = [m["expense"] for m in d["monthly_series"]]

    warnings_html = ""
    if warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in warnings)
        warnings_html = f"""
  <div class="banner banner-warn">
    <strong>{len(warnings)} data check warning{'s' if len(warnings) != 1 else ''} from the last load</strong>
    <ul style="margin:6px 0 0; padding-left:18px;">{items}</ul>
  </div>"""

    if d["has_investments"]:
        drawdowns = [r for r in d["investment_months"] if r["drawdown"]]
        total_inv = d["total_investment_all_time"]
        share = (total_inv / d["total_income_all_time"]) if d["total_income_all_time"] else None
        note = (f"{len(drawdowns)} month{'' if len(drawdowns) == 1 else 's'} contributed more than "
                f"the month netted — funded from savings, not income."
                if drawdowns else
                "Every funded month came out of that month's income.")
        investing_html = f"""
  <div class="card">
    <h2>Cash flow vs investing</h2>
    <div class="card-sub">{esc(note)} Full breakdown on the
      <a href="investments.html">Investments</a> tab.</div>
    <div class="tiles" style="margin-bottom:0;">
      {render_stat_tile_static("Contributed all time", money(total_inv), "var(--series-1)")}
      {render_stat_tile_static("Share of income", pct(share))}
      {render_stat_tile_static("Drawdown months", str(len(drawdowns)),
                                "var(--warning)" if drawdowns else None)}
    </div>
  </div>"""
    else:
        investing_html = """
  <div class="banner">No investment contributions in the ledger yet — add them in the
    <a href="import.html">Import</a> tab and the cash-flow-vs-investing view on the
    <a href="investments.html">Investments</a> tab fills in.</div>"""

    body = f"""
  <div class="page-header">
    <h1>Dashboard</h1>
    <div class="subtitle">{d['n_months']} months loaded, latest {d['latest_date'].strftime('%B %-d, %Y')}</div>
  </div>
  {warnings_html}

  <div class="month-toggle">
    <label for="month-select">Month</label>
    <select id="month-select"></select>
  </div>

  <div class="status-row">
    <div class="status-card" id="sc-baseline">
      <div class="sc-head">
        <span class="sc-label">Baseline</span>
        <span class="status-badge neutral m-badge"></span>
      </div>
      <div class="sc-value" id="sc-baseline-value">—</div>
      <div class="sc-track"><div class="sc-fill" id="sc-baseline-fill" style="width:0%"></div></div>
      <div class="sc-note" id="sc-baseline-note"></div>
    </div>

    <div class="status-card" id="sc-discretionary">
      <div class="sc-head">
        <span class="sc-label">Discretionary pool</span>
        <span class="status-badge neutral m-badge"></span>
      </div>
      <div class="sc-value" id="sc-discretionary-value">—</div>
      <div class="sc-track"><div class="sc-fill" id="sc-discretionary-fill" style="width:0%"></div></div>
      <div class="sc-note" id="sc-discretionary-note">
        <a href="budget.html">Full breakdown on the Budget page →</a>
      </div>
    </div>
  </div>

  <div class="card">
    <h2 id="h-dash-topcats">Category budget status</h2>
    <div class="card-sub">% of each category's monthly target spent so far this month (not pace-adjusted —
      just actual ÷ target). Sorted by budget size, largest first. Full detail, and editable targets, on
      the <a href="budget.html">Budget page</a>.</div>
    <div id="dash-top-cats"></div>
  </div>

  <div class="card">
    <h2>Monthly trend</h2>
    <div class="card-sub">The shaded gap between the lines is net cash flow — green where income is ahead, red
      where expenses caught up or passed it. Click a point to jump the month toggle there.</div>
    <div class="legend">
      <span class="legend-key"><span class="legend-swatch" style="background:var(--series-1)"></span>Income</span>
      <span class="legend-key"><span class="legend-swatch" style="background:var(--series-2)"></span>Expenses</span>
      <span class="legend-key"><span class="legend-swatch" style="background:var(--text-secondary); border-top:2px dashed var(--text-secondary); height:0;"></span>Net</span>
    </div>
    {render_trend_with_gap_svg("trend", months_lbl, income_vals, expense_vals)}
  </div>

  <div class="card">
    <h2>Year to date</h2>
    <div class="card-sub">{d['n_months']} months, {esc(month_label(d['months'][0]))} through {esc(month_label(d['months'][-1]))}.</div>
    <div class="tiles">
      {render_stat_tile_static("YTD income", money(sum(m["income"] for m in d["monthly_series"])), "var(--series-1)")}
      {render_stat_tile_static("YTD expenses", money(sum(m["expense"] for m in d["monthly_series"])), "var(--series-2)")}
      {render_stat_tile_static("YTD net", money(sum(m["income"] - m["expense"] for m in d["monthly_series"])), "var(--good)")}
    </div>
  </div>

  {investing_html}

  <footer class="page-footer">Built from data/budget.db · not committed, not shared ·
    budget edits are saved in this browser only (<button class="reset-btn" id="reset-btn" style="margin-left:6px">reset to config defaults</button>)</footer>"""

    init_js = """
const CATEGORY_LABELS_DASH = """ + json.dumps(CATEGORY_LABELS) + """;
function renderDashboard(monthIdx) {
  const DATA = window.DATA;
  const month = DATA.months[monthIdx];
  const m = DATA.byMonth[month];
  const label = DATA.monthLabels[month];

  document.getElementById('h-dash-topcats').textContent = 'Category budget status — ' + label;

  const baseline = baselineSummary(DATA, monthIdx);
  let baseStatus = 'neutral';
  if (baseline.withTarget) {
    baseStatus = baseline.onTrack === baseline.withTarget ? 'good' : (baseline.onTrack === 0 ? 'critical' : 'warning');
  }
  const baseColor = STATUS_HEX[baseStatus];
  document.getElementById('sc-baseline-value').textContent = fmtMoney(baseline.actual) + ' / ' + fmtMoney(baseline.target);
  document.getElementById('sc-baseline').style.setProperty('--fill-color', baseColor);
  document.getElementById('sc-baseline-fill').style.width =
    (baseline.withTarget ? (baseline.onTrack / baseline.withTarget) * 100 : 0) + '%';
  document.getElementById('sc-baseline-fill').style.background = baseColor;
  document.getElementById('sc-baseline-note').textContent = baseline.onTrack + ' of ' + baseline.withTarget + ' categories on track this month';
  const baseBadge = document.querySelector('#sc-baseline .m-badge');
  baseBadge.textContent = baseline.withTarget ? STATUS_TEXT[baseStatus] : '';
  baseBadge.className = 'status-badge m-badge ' + baseStatus;

  const discPool = poolResult(DATA, monthIdx, 'discretionary');
  const discColor = STATUS_HEX[discPool.status];
  document.getElementById('sc-discretionary-value').textContent = fmtMoney(discPool.actual) + ' / ' + fmtMoney(discPool.target);
  document.getElementById('sc-discretionary').style.setProperty('--fill-color', discColor);
  document.getElementById('sc-discretionary-fill').style.width = Math.min(100, (discPool.ratio || 0) * 100) + '%';
  document.getElementById('sc-discretionary-fill').style.background = discColor;
  const discBadge = document.querySelector('#sc-discretionary .m-badge');
  discBadge.textContent = STATUS_TEXT[discPool.status];
  discBadge.className = 'status-badge m-badge ' + discPool.status;

  renderCategoryPercentList(document.getElementById('dash-top-cats'), DATA, monthIdx, CATEGORY_LABELS_DASH);
}
populateMonthSelect(document.getElementById('month-select'), window.DATA, renderDashboard);
renderDashboard(window.DATA.months.length - 1);
document.getElementById('reset-btn').addEventListener('click', () => { clearOverrides(); renderDashboard(Number(document.getElementById('month-select').value)); });
"""
    return render_shell("Dashboard", "index.html", body, d["client_data"], init_js)


# --------------------------------------------------------- page: expenses

def page_expenses(d):
    # Total vs underlying spend (SPEC §5). Underlying drops the `lumpy`
    # categories — the ones saved up for over months and then spent at once —
    # so the trend line isn't dominated by when a flight happened to be
    # booked. Total keeps the de-emphasis ink and underlying takes the
    # expense accent, the same contract render_sparkline() uses: context
    # recessive, the figure you actually read in the accent hue. The pair
    # was checked with the dataviz validator in both themes (CVD ΔE 10.7
    # light / 11.0 dark, normal-vision 21.7 / 16.6, contrast ≥3:1 both) —
    # its one FAIL is the chroma floor, which is the *point* of a
    # de-emphasis ink and is out of that check's categorical-palette scope.
    # With nothing lumpy the two lines would be identical, so fall back to
    # the single-series chart (and no legend, per the >=2-series rule).
    lumpy_labels = [CATEGORY_LABELS.get(c, c).lower() for c in d["lumpy_cats"]]
    total_vals = [m["expense"] for m in d["monthly_series"]]
    exp_trend_legend = ""
    exp_trend_sub = "Click a point to jump the month toggle there."
    if not lumpy_labels:
        # One series, so it takes the accent (a lone line is never recessive)
        # and needs no legend - the heading names it.
        exp_trend_series = [dict(name="Expenses", color="var(--series-2)", values=total_vals)]
    else:
        exp_trend_series = [
            dict(name="Total", color="var(--text-muted)", values=total_vals),
            dict(name="Underlying", color="var(--series-2)",
                 values=[m["underlying"] for m in d["monthly_series"]]),
        ]
        exp_trend_sub = (
            f"<strong>Underlying</strong> is total spend minus {and_list(lumpy_labels)} — "
            "lumpy spending, saved up across months and then spent in one go, so it says more about "
            "when something was booked than about the month it lands in. Read the trend off the "
            "underlying line; the total is what actually left the account. Click a point to jump the "
            "month toggle there.")
        exp_trend_legend = """
    <div class="legend">
      <span class="legend-key"><span class="legend-swatch" style="background:var(--series-2)"></span>Underlying</span>
      <span class="legend-key"><span class="legend-swatch" style="background:var(--text-muted)"></span>Total</span>
    </div>"""
    body = f"""
  <div class="page-header">
    <h1>Expenses</h1>
    <div class="subtitle">{d['n_months']} months loaded · {money(d['total_expense_all_time'])} all-time</div>
  </div>

  <div class="month-toggle">
    <label for="month-select">Month</label>
    <select id="month-select"></select>
  </div>

  <div class="card">
    <h2 id="h-cumulative">Discretionary: cumulative spend vs. pace</h2>
    <div class="card-sub">
      <span class="legend-key"><span class="legend-swatch" style="background:var(--series-2)"></span>Running total</span>
      &nbsp;&nbsp;<span class="legend-key"><span class="legend-swatch" style="background:var(--text-muted); border-top:2px dashed var(--text-muted); height:0;"></span>Even pace toward {esc(money(sum(d["targets"].get(c, 0) for c in d["discretionary_order"])))}/mo (discretionary only)</span>
    </div>
    <div id="cumulative-chart"></div>
  </div>

  <div class="card">
    <h2 id="h-purchases">Biggest purchases</h2>
    <div id="top-purchases"></div>
  </div>

  <div class="card">
    <h2 id="h-bycat">By category</h2>
    <div class="card-sub">Click a category to filter the transaction list below to just that category.</div>
    <div id="month-bars"></div>
  </div>

  <div class="card">
    <h2>Monthly expense trend</h2>
    <div class="card-sub">{exp_trend_sub}</div>
    {exp_trend_legend}
    {render_line_svg("exp-trend", [month_label(m["month"]) for m in d["monthly_series"]],
                      exp_trend_series,
                      lambda v: f"{v/1000:.1f}k", money)}
  </div>

  <div class="card">
    <h2>By category — all time</h2>
    {render_bar_chart_svg(d["all_time_expense_ranked"], "var(--series-2)")}
  </div>

  <div class="card">
    <h2>Transactions</h2>
    <input type="search" id="txn-search" class="txn-search" placeholder="Search all months by description or amount…">
    <div id="txn-filter"></div>
    <div class="search-note" id="search-note"></div>
    <div id="month-txns"></div>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""

    init_js = f"""
const CATEGORY_LABELS = {json.dumps(CATEGORY_LABELS)};
function labelFor(cat) {{ return CATEGORY_LABELS[cat] || cat; }}
let selectedCategory = null;
let currentMonthIdx = window.DATA.months.length - 1;

function renderFilterChip() {{
  const el = document.getElementById('txn-filter');
  el.textContent = '';
  if (!selectedCategory) return;
  const chip = document.createElement('div');
  chip.className = 'filter-chip';
  const span = document.createElement('span');
  span.textContent = 'Showing ' + labelFor(selectedCategory) + ' only';
  const btn = document.createElement('button');
  btn.textContent = '\\u00d7';
  btn.setAttribute('aria-label', 'Clear filter');
  btn.addEventListener('click', () => {{ selectedCategory = null; renderExpenses(currentMonthIdx); }});
  chip.appendChild(span); chip.appendChild(btn);
  el.appendChild(chip);
}}

function renderTxnSection() {{
  const DATA = window.DATA;
  const query = document.getElementById('txn-search').value;
  const searchResults = searchTransactions(DATA, 'expenseTxns', query);
  renderFilterChip();
  const noteEl = document.getElementById('search-note');
  if (searchResults) {{
    const filtered = selectedCategory ? searchResults.filter(t => t.category === selectedCategory) : searchResults;
    noteEl.textContent = filtered.length + (filtered.length === 1 ? ' match' : ' matches') + ' across all months';
    renderTxnTable(document.getElementById('month-txns'), filtered, labelFor,
      {{ showMonth: true, monthLabels: DATA.monthLabels, emptyText: 'No matches.' }});
  }} else {{
    noteEl.textContent = '';
    const month = DATA.months[currentMonthIdx];
    const m = DATA.byMonth[month];
    const filtered = selectedCategory ? m.expenseTxns.filter(t => t.category === selectedCategory) : m.expenseTxns;
    renderTxnTable(document.getElementById('month-txns'), filtered, labelFor);
  }}
}}
document.getElementById('txn-search').addEventListener('input', renderTxnSection);

function renderExpenses(monthIdx) {{
  currentMonthIdx = monthIdx;
  const DATA = window.DATA;
  const month = DATA.months[monthIdx];
  const m = DATA.byMonth[month];
  const label = DATA.monthLabels[month];
  document.getElementById('h-cumulative').textContent = 'Discretionary: cumulative spend vs. pace — ' + label;
  document.getElementById('h-purchases').textContent = 'Biggest purchases — ' + label;
  document.getElementById('h-bycat').textContent = 'By category — ' + label;
  const ranked = Object.entries(m.expenseByCat).sort((a,b) => b[1]-a[1]).map(([c,v]) => [c, labelFor(c), v]);
  renderBarList(document.getElementById('month-bars'), ranked, 'var(--series-2)', fmtMoney, {{
    onClick: (catKey) => {{ selectedCategory = (selectedCategory === catKey) ? null : catKey; renderExpenses(currentMonthIdx); }},
    selectedKey: selectedCategory,
  }});
  renderCumulativeChart(document.getElementById('cumulative-chart'), DATA, monthIdx, DATA.order.discretionary);
  renderTopPurchases(document.getElementById('top-purchases'), m.expenseTxns, labelFor, 5);
  renderTxnSection();
}}
populateMonthSelect(document.getElementById('month-select'), window.DATA, renderExpenses);
renderExpenses(currentMonthIdx);
"""
    return render_shell("Expenses", "expenses.html", body, d["client_data"], init_js)


# ----------------------------------------------------------- page: income

def page_income(d):
    body = f"""
  <div class="page-header">
    <h1>Income</h1>
    <div class="subtitle">{d['n_months']} months loaded · {money(d['total_income_all_time'])} all-time</div>
  </div>

  <div class="month-toggle">
    <label for="month-select">Month</label>
    <select id="month-select"></select>
  </div>

  <div class="card">
    <h2>Monthly income trend</h2>
    <div class="card-sub">Click a point to jump the month toggle there.</div>
    {render_line_svg("inc-trend", [month_label(m["month"]) for m in d["monthly_series"]],
                      [dict(name="Income", color="var(--series-1)", values=[m["income"] for m in d["monthly_series"]])],
                      lambda v: f"{v/1000:.1f}k", money)}
  </div>

  <div class="card">
    <h2 id="h-bysource">By source</h2>
    <div class="card-sub">Click a source to filter the transaction list below to just that source.</div>
    <div id="month-bars"></div>
  </div>

  <div class="card">
    <h2>By source — all time</h2>
    {render_bar_chart_svg(d["all_time_income_ranked"], "var(--series-1)")}
  </div>

  <div class="card">
    <h2>Transactions</h2>
    <input type="search" id="txn-search" class="txn-search" placeholder="Search all months by description or amount…">
    <div id="txn-filter"></div>
    <div class="search-note" id="search-note"></div>
    <div id="month-txns"></div>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""

    init_js = f"""
const INCOME_LABELS = {json.dumps({**INCOME_LABELS, **TRANSFER_LABELS})};
function labelFor(cat) {{ return INCOME_LABELS[cat] || cat; }}
let selectedCategory = null;
let currentMonthIdx = window.DATA.months.length - 1;

function renderFilterChip() {{
  const el = document.getElementById('txn-filter');
  el.textContent = '';
  if (!selectedCategory) return;
  const chip = document.createElement('div');
  chip.className = 'filter-chip';
  const span = document.createElement('span');
  span.textContent = 'Showing ' + labelFor(selectedCategory) + ' only';
  const btn = document.createElement('button');
  btn.textContent = '\\u00d7';
  btn.setAttribute('aria-label', 'Clear filter');
  btn.addEventListener('click', () => {{ selectedCategory = null; renderIncome(currentMonthIdx); }});
  chip.appendChild(span); chip.appendChild(btn);
  el.appendChild(chip);
}}

function renderTxnSection() {{
  const DATA = window.DATA;
  const query = document.getElementById('txn-search').value;
  const searchResults = searchTransactions(DATA, 'incomeTxns', query);
  renderFilterChip();
  const noteEl = document.getElementById('search-note');
  if (searchResults) {{
    const filtered = selectedCategory ? searchResults.filter(t => t.category === selectedCategory) : searchResults;
    noteEl.textContent = filtered.length + (filtered.length === 1 ? ' match' : ' matches') + ' across all months';
    renderTxnTable(document.getElementById('month-txns'), filtered, labelFor,
      {{ showMonth: true, monthLabels: DATA.monthLabels, emptyText: 'No matches.' }});
  }} else {{
    noteEl.textContent = '';
    const month = DATA.months[currentMonthIdx];
    const m = DATA.byMonth[month];
    const filtered = selectedCategory ? m.incomeTxns.filter(t => t.category === selectedCategory) : m.incomeTxns;
    renderTxnTable(document.getElementById('month-txns'), filtered, labelFor);
  }}
}}
document.getElementById('txn-search').addEventListener('input', renderTxnSection);

function renderIncome(monthIdx) {{
  currentMonthIdx = monthIdx;
  const DATA = window.DATA;
  const month = DATA.months[monthIdx];
  const m = DATA.byMonth[month];
  document.getElementById('h-bysource').textContent = 'By source — ' + DATA.monthLabels[month];
  const ranked = Object.entries(m.incomeByCat).sort((a,b) => b[1]-a[1]).map(([c,v]) => [c, labelFor(c), v]);
  renderBarList(document.getElementById('month-bars'), ranked, 'var(--series-1)', fmtMoney, {{
    onClick: (catKey) => {{ selectedCategory = (selectedCategory === catKey) ? null : catKey; renderIncome(currentMonthIdx); }},
    selectedKey: selectedCategory,
  }});
  renderTxnSection();
}}
populateMonthSelect(document.getElementById('month-select'), window.DATA, renderIncome);
renderIncome(currentMonthIdx);
"""
    return render_shell("Income", "income.html", body, d["client_data"], init_js)



def page_investments(d):
    """SPEC §7.4, answered (2026-10-14): contributions are real dated rows
    with kind=investment, so this page derives everything from the ledger
    like every other view — no stored monthly totals, no screenshot numbers
    transcribed into config."""
    # Only accounts that actually have contributions get a hue, a stack slot
    # and a legend key — a legend describes the chart, not the taxonomy, and
    # an account with no rows would read as a real $0 slice. Adding the first
    # crypto row brings crypto back on the next build, in taxonomy order.
    funded_accounts = {cat for cat, _ in d["all_time_investment_ranked"]}
    keys = [k for k in INVESTMENT_LABELS if k in funded_accounts] or list(INVESTMENT_LABELS)
    months_lbl = [month_label(m["month"]) for m in d["monthly_series"]]

    if not d["has_investments"]:
        accounts = ", ".join(INVESTMENT_LABELS[k].lower() for k in keys)
        body = f"""
  <div class="page-header">
    <h1>Investments</h1>
    <div class="subtitle">How much goes into savings and investments each month</div>
  </div>

  <div class="card">
    <h2>No contributions in the ledger yet</h2>
    <div class="card-sub">
      This page charts <code>kind=investment</code> rows — {esc(accounts)} — straight from the
      transaction ledger, the same way every other page works. Nothing is charted until there are
      real rows to chart: a contribution needs a date and an amount, so it can be paced against
      the month's net cash flow (SPEC §5).
    </div>
    <div class="card-sub" style="margin-top:10px;">
      To add them, tell Claude the contributions in chat (or hand it a brokerage screenshot) and
      approve the staged rows in the <a href="import.html">Import</a> tab.
    </div>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""
        return render_shell("Investments", "investments.html", body, d["client_data"], "")

    inv_vals = [m["investment"] for m in d["monthly_series"]]
    net_vals = [m["income"] - m["expense"] for m in d["monthly_series"]]
    values_by_month = [d["client_data"]["byMonth"][m]["investmentByCat"] for m in d["months"]]
    total = d["total_investment_all_time"]
    funded = [r for r in d["investment_months"] if r["contributed"] > 0]

    # One tile per account. The total is derived from the contributions, so it
    # needs no maintenance and can't go stale. A recorded market value in
    # config/accounts.yml takes the headline instead, with its as_of date and
    # the contributed figure underneath — only then are the two different.
    account_tiles = []
    for a in d["account_rollup"]:
        if a["balance"] is not None:
            as_of = a["as_of"]
            dated = ("as of " + (as_of.strftime("%b %-d, %Y") if hasattr(as_of, "strftime") else str(as_of))
                     if as_of else "market value")
            value, sub = money(a["balance"]), f"{dated} · {money(a['contributed'])} contributed"
        else:
            value, sub = money(a["contributed"]), "contributed to date"
        account_tiles.append(render_stat_tile_static(
            a["label"], value, INVESTMENT_COLORS.get(a["key"]), sub=sub))

    body = f"""
  <div class="page-header">
    <h1>Investments</h1>
    <div class="subtitle">{money(total)} contributed over {len(funded)} funded month{'' if len(funded) == 1 else 's'} ·
      {esc(", ".join(INVESTMENT_LABELS[k].lower() for k in keys))}</div>
  </div>

  <div class="month-toggle">
    <label for="month-select">Month</label>
    <select id="month-select"></select>
  </div>

  <div class="tiles">
    {render_stat_tile("inv-month", "Contributed", "var(--series-1)", with_delta=True,
                      sparkline_values=inv_vals, spark_color="var(--series-1)")}
    {"".join(account_tiles)}
    {render_stat_tile_static("Contributed all time", money(total))}
  </div>

  <div class="card">
    <h2>Monthly contributions by account</h2>
    <div class="card-sub">Hover a month for the split; click it to move the month toggle there.</div>
    {render_legend([(INVESTMENT_LABELS[k], INVESTMENT_COLORS[k]) for k in keys])}
    {render_stacked_bar_svg("inv-stack", months_lbl, keys, values_by_month, INVESTMENT_COLORS)}
  </div>

  <div class="card">
    <h2>Funded out of income, or out of savings?</h2>
    <div class="card-sub">Contributions against net cash flow (income − expenses) for the same month.
      Where the solid line is above the dashed one, the month invested more than it earned.</div>
    {render_legend([("Contributed", "var(--series-1)"), ("Net cash flow", "var(--text-secondary)")], dashed_last=True)}
    {render_line_svg("inv-vs-net", months_lbl,
                     [dict(name="Contributed", color="var(--series-1)", values=inv_vals),
                      dict(name="Net cash flow", color="var(--text-secondary)", values=net_vals, dash=True)],
                     lambda v: f"{v/1000:.1f}k", money)}
  </div>

  <div class="card">
    <h2>Mix — all time</h2>
    <div class="card-sub">The actual split. The source Budget sheet's 70/20/10
      brokerage/emergency/crypto is descriptive, not an allocation target, so there's no target
      line here to miss.</div>
    {render_legend([(INVESTMENT_LABELS[k], INVESTMENT_COLORS[k]) for k in keys])}
    {render_mix_bar(d["all_time_investment_ranked"], INVESTMENT_COLORS)}
  </div>

  <div class="card">
    <h2 id="h-inv-bycat">By account</h2>
    <div id="inv-month-bars"></div>
  </div>

  <div class="card">
    <h2>Contributions</h2>
    <input type="search" id="txn-search" class="txn-search" placeholder="Search all months by description or amount…">
    <div class="search-note" id="search-note"></div>
    <div id="month-txns"></div>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""

    init_js = """
const INV_CATS = window.DATA.investmentCategories;
function labelFor(cat) { return (INV_CATS[cat] || {}).label || cat; }
function colorFor(cat) { return (INV_CATS[cat] || {}).color || 'var(--series-1)'; }
let currentMonthIdx = window.DATA.months.length - 1;

function renderTxnSection() {
  const DATA = window.DATA;
  const query = document.getElementById('txn-search').value;
  const searchResults = searchTransactions(DATA, 'investmentTxns', query);
  const noteEl = document.getElementById('search-note');
  if (searchResults) {
    noteEl.textContent = searchResults.length + (searchResults.length === 1 ? ' match' : ' matches') + ' across all months';
    renderTxnTable(document.getElementById('month-txns'), searchResults, labelFor,
      { showMonth: true, monthLabels: DATA.monthLabels, emptyText: 'No matches.' });
  } else {
    noteEl.textContent = '';
    const m = DATA.byMonth[DATA.months[currentMonthIdx]];
    renderTxnTable(document.getElementById('month-txns'), m.investmentTxns, labelFor,
      { emptyText: 'No contributions this month.' });
  }
}
document.getElementById('txn-search').addEventListener('input', renderTxnSection);

function renderInvestments(monthIdx) {
  currentMonthIdx = monthIdx;
  const DATA = window.DATA;
  const month = DATA.months[monthIdx];
  const m = DATA.byMonth[month];
  const prev = monthIdx > 0 ? DATA.byMonth[DATA.months[monthIdx - 1]] : null;

  document.getElementById('inv-month-value').textContent = fmtMoney(m.investment);
  document.getElementById('inv-month-sub').textContent = m.income
    ? fmtPct(m.investment / m.income) + ' of ' + DATA.monthLabels[month] + ' income'
    : 'no income recorded this month';
  renderDelta('inv-month-delta', m.investment, prev ? prev.investment : undefined, { higherIsBetter: true });

  document.getElementById('h-inv-bycat').textContent = 'By account — ' + DATA.monthLabels[month];
  const ranked = Object.entries(m.investmentByCat).sort((a, b) => b[1] - a[1]).map(([c, v]) => [c, labelFor(c), v]);
  renderBarList(document.getElementById('inv-month-bars'), ranked, null, fmtMoney, { colorFor });
  renderTxnSection();
}
populateMonthSelect(document.getElementById('month-select'), window.DATA, renderInvestments);
renderInvestments(currentMonthIdx);
"""
    return render_shell("Investments", "investments.html", body, d["client_data"], init_js)


# ----------------------------------------------------------- page: budget

def page_budget(d):
    baseline_meters = "".join(
        render_meter_skeleton(c, CATEGORY_LABELS[c], d["cadence_of"][c]) for c in d["baseline_order"])
    pooled_meters = "".join(
        render_meter_skeleton(c, CATEGORY_LABELS[c], d["cadence_of"][c]) for c in d["pooled_discretionary"])
    lumpy_meters = "".join(
        render_meter_skeleton(c, CATEGORY_LABELS[c], d["cadence_of"][c]) for c in d["lumpy_discretionary"])
    # Only mention the lumpy card when there is one — every discretionary
    # category is `variable` as of 2026-08-11, so it usually renders nothing.
    pool_scope_note = ("Only variable-cadence categories are pooled here — lumpy ones are tracked separately below."
                       if d["lumpy_discretionary"] else
                       "Every discretionary category is variable-cadence right now, so all of them are pooled here.")

    lumpy_card = "" if not d["lumpy_discretionary"] else f"""
  <div class="card">
    <h2>Lumpy / one-off spending — saved up for, not paced</h2>
    <div class="card-sub">These don't pace evenly within a month by design (SPEC §5: "a trip is one decision, not a
      monthly overrun") — pooling them would make one early-month purchase read as the whole pool being over pace.
      Each is run as a <strong>sinking fund</strong> instead: the monthly target accrues into a bucket and spending
      draws it down, so the number to read is the balance, not a percentage.</div>
    {lumpy_meters}
  </div>"""

    # One chart per lumpy category: the bucket level over time. A single
    # balance series rather than accrued-vs-spent as two lines, because the
    # level *is* the answer to "how much is in there" — and render_line_svg
    # draws the zero baseline as soon as the domain crosses it, which is
    # what makes an overdraw legible at a glance. Server-rendered from the
    # config target, so it does not follow a browser-local target edit —
    # the same contract the pool-history chart below it already has.
    fund_charts = ""
    for c in d["lumpy_discretionary"]:
        fund = d["lumpy_funds"].get(c)
        if not fund:
            continue
        label = CATEGORY_LABELS.get(c, c)
        rate, end = d["targets"].get(c) or 0, fund[-1]["balance"]
        state = f"{money(end)} in the bucket" if end >= 0 else f"{money(-end)} overdrawn"
        fund_charts += f"""
  <div class="card">
    <h2>{esc(label)} fund — accrued vs drawn down</h2>
    <div class="card-sub">{esc(money(rate))}/mo accrues into the bucket from {esc(month_label(fund[0]["month"]))};
      every {esc(label.lower())} charge draws it back down. The line is the balance, so it climbs through quiet
      months and drops on the day something gets booked — currently <strong>{esc(state)}</strong>. Below the zero
      line means spending has outrun the accrual: a cumulative overrun, not a monthly one.</div>
    {render_line_svg("fund-" + c, [month_label(f["month"]) for f in fund],
                      [dict(name="Bucket balance", color="var(--series-2)",
                            values=[f["balance"] for f in fund])],
                      money_compact, money)}
  </div>"""

    history_rows = "".join(f"""<tr>
      <td>{esc(month_label(h['month']))}</td>
      <td class="num">{esc(money(h['actual']))}</td>
      <td class="num">{esc(money(h['target']))}</td>
      <td class="num"><span class="status-badge {h['status']}">{STATUS_TEXT[h['status']]}</span></td>
    </tr>""" for h in reversed(d["monthly_pool_history"]))
    history_series = [dict(name="Discretionary spend vs target", color="var(--series-1)",
                            values=[(h["ratio"] or 0) * 100 for h in d["monthly_pool_history"]])]

    body = f"""
  <div class="page-header">
    <h1>Budget — monthly goals</h1>
    <div class="subtitle">Scenario A targets, editable below · edits save to this browser only</div>
  </div>

  <div class="month-toggle">
    <label for="month-select">Month</label>
    <select id="month-select"></select>
    <span class="edit-hint" id="month-progress"></span>
    <button class="reset-btn" id="reset-btn" style="margin-left:auto">reset targets to config defaults</button>
  </div>

  <div class="card">
    <h2>Baseline — checked per category</h2>
    <div class="card-sub">Fixed = paid or not. Variable = pace-adjusted against day of month. Click a target to edit it.</div>
    {baseline_meters}
  </div>

  <div class="card">
    <h2>Discretionary — day to day, checked as one pool</h2>
    <div class="card-sub">Individual categories swing a lot month to month; the pooled total is the real constraint (SPEC §5).
      {pool_scope_note}</div>
    {render_pool_skeleton("pool-discretionary", "Variable-cadence discretionary spend, pooled")}
    <div class="group-label">Individual categories — guides, edit targets here, pool above reflects them</div>
    {pooled_meters}
  </div>

  {lumpy_card}
  {fund_charts}

  <div class="card">
    <h2>Discretionary pool, over time</h2>
    <div class="card-sub">Variable-cadence categories only, same as the pool above. 100% = right on target for that
      month. Uses config defaults, not your edits above (historical view). Click a point to jump the month toggle there.</div>
    {render_line_svg("pool-hist", [month_label(h["month"]) for h in d["monthly_pool_history"]],
                      history_series, lambda v: f"{v:.0f}%", lambda v: f"{v:.0f}%")}
    <table class="data-table" style="margin-top:14px">
      <thead><tr><th>Month</th><th class="num">Spend</th><th class="num">Target</th><th class="num">Status</th></tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""

    init_js = """
let currentMonthIdx = window.DATA.months.length - 1;

function renderBudgetPage(monthIdx) {
  currentMonthIdx = monthIdx;
  const DATA = window.DATA;
  const month = DATA.months[monthIdx];
  document.getElementById('month-progress').textContent = (monthIdx === DATA.months.length - 1)
    ? 'day ' + DATA.dayOfMonth + ' · ' + Math.round(DATA.livePace * 100) + '% through the month'
    : 'month complete';

  [...DATA.order.baseline, ...DATA.order.discretionary].forEach(cat => {
    const row = document.getElementById('meter-' + cat);
    if (row) renderMeterRow(row, categoryResult(DATA, cat, monthIdx));
  });
  renderPoolRow(document.getElementById('pool-discretionary'), poolResult(DATA, monthIdx, 'discretionary'));
}

document.querySelectorAll('.target-input').forEach(input => {
  input.addEventListener('input', () => {
    setOverride(input.dataset.cat, input.value === '' ? null : input.value);
    renderBudgetPage(currentMonthIdx);
  });
});
document.getElementById('reset-btn').addEventListener('click', () => {
  clearOverrides();
  renderBudgetPage(currentMonthIdx);
});
populateMonthSelect(document.getElementById('month-select'), window.DATA, renderBudgetPage);
renderBudgetPage(currentMonthIdx);
"""
    return render_shell("Budget", "budget.html", body, d["client_data"], init_js)


# ----------------------------------------------------------- page: import

def page_import(d):
    """The one interactive page in an otherwise static site (SPEC §10,
    scoped down — see CLAUDE.md). Renders a shell identical to the other
    pages, but the transaction list isn't in window.DATA: it's fetched live
    from scripts/import_server.py, since these rows change without a full
    dashboard rebuild. Only works served over http://localhost — opened via
    file:// like the rest of the site, the fetch calls fail (no server to
    fetch from), which is why the banner below is unconditional, not
    warning-gated like the load-warnings banner on the Dashboard page."""
    body = """
  <div class="page-header">
    <h1>Import</h1>
    <div class="subtitle">Review transactions Claude staged from a screenshot or file before they land in the ledger</div>
  </div>

  <div class="banner banner-warn">
    <strong>Needs the local import server running</strong>
    <div style="margin-top:4px;">In a terminal: <code>python scripts/import_server.py</code>, then open
    <code>http://localhost:8765/import.html</code> — this page does nothing useful opened directly from disk.</div>
  </div>

  <div class="card">
    <h2>How this works</h2>
    <div class="card-sub">
      Give Claude a screenshot or file in chat and ask it to import your transactions. Claude reads it,
      matches each transaction to your category taxonomy, and stages the candidates below for you to check —
      nothing touches the ledger until you approve a row here.
    </div>
  </div>

  <div class="card">
    <h2>Pending review <span id="pending-count" class="card-sub"></span></h2>
    <div id="pending-list"></div>
  </div>

  <footer class="page-footer">Built from data/budget.db · not committed, not shared</footer>"""

    init_js = """
const CATEGORY_LABELS = window.DATA.categories;
const INCOME_LABELS = window.DATA.incomeCategories;

const INVESTMENT_LABELS = window.DATA.investmentCategories;

function categoryOptionsFor(kind, selected) {
  const source = kind === 'expense' ? CATEGORY_LABELS
               : kind === 'investment' ? INVESTMENT_LABELS
               : INCOME_LABELS;
  return Object.entries(source).map(([slug, info]) =>
    `<option value="${slug}" ${slug === selected ? 'selected' : ''}>${info.label}</option>`).join('');
}

async function loadPending() {
  const listEl = document.getElementById('pending-list');
  const countEl = document.getElementById('pending-count');
  let rows;
  try {
    rows = await (await fetch('/api/pending')).json();
  } catch (e) {
    listEl.innerHTML = '<p class="card-sub">Can\\'t reach the import server — make sure it\\'s running and you opened this page via http://localhost:8765, not a file:// path.</p>';
    countEl.textContent = '';
    return;
  }
  countEl.textContent = rows.length ? `(${rows.length})` : '';
  if (!rows.length) {
    listEl.innerHTML = '<p class="card-sub">Nothing waiting on review.</p>';
    return;
  }
  listEl.innerHTML = `<table class="data-table">
    <thead><tr><th>Date</th><th>Description</th><th>Kind</th><th>Category</th><th class="num">Amount</th><th>Source</th><th></th></tr></thead>
    <tbody>${rows.map(r => `
      <tr data-id="${r.id}">
        <td><input type="date" class="f-date" value="${r.date}"></td>
        <td><input type="text" class="f-description" value="${r.description.replace(/"/g,'&quot;')}"></td>
        <td>
          <select class="f-kind">
            <option value="expense" ${r.kind==='expense'?'selected':''}>Expense</option>
            <option value="income" ${r.kind==='income'?'selected':''}>Income</option>
            <option value="transfer" ${r.kind==='transfer'?'selected':''}>Transfer</option>
            <option value="investment" ${r.kind==='investment'?'selected':''}>Investment</option>
          </select>
        </td>
        <td><select class="f-category">${categoryOptionsFor(r.kind, r.category)}</select></td>
        <td class="num"><input type="number" step="0.01" class="f-amount" value="${r.amount}" style="width:90px; text-align:right;"></td>
        <td class="card-sub">${(r.source_note||'').replace(/</g,'&lt;')}</td>
        <td style="white-space:nowrap;">
          <button class="btn btn-approve">Approve</button>
          <button class="btn btn-reject">Reject</button>
        </td>
      </tr>`).join('')}</tbody></table>`;

  listEl.querySelectorAll('tr[data-id]').forEach(tr => {
    const kindSel = tr.querySelector('.f-kind');
    kindSel.addEventListener('change', () => {
      tr.querySelector('.f-category').innerHTML = categoryOptionsFor(kindSel.value, null);
    });
    tr.querySelector('.btn-approve').addEventListener('click', async () => {
      const id = tr.dataset.id;
      const payload = {
        id,
        date: tr.querySelector('.f-date').value,
        description: tr.querySelector('.f-description').value,
        kind: tr.querySelector('.f-kind').value,
        category: tr.querySelector('.f-category').value,
        amount: tr.querySelector('.f-amount').value,
      };
      const res = await fetch('/api/approve', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
      const out = await res.json();
      if (!res.ok) { alert('Approve failed: ' + out.error); return; }
      loadPending();
    });
    tr.querySelector('.btn-reject').addEventListener('click', async () => {
      const id = tr.dataset.id;
      const res = await fetch('/api/reject', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ id }) });
      const out = await res.json();
      if (!res.ok) { alert('Reject failed: ' + out.error); return; }
      loadPending();
    });
  });
}
loadPending();
"""
    return render_shell("Import", "import.html", body, d["client_data"], init_js)


# ---------------------------------------------------------------- main ----

def main():
    if not DB_PATH.exists():
        raise SystemExit("data/budget.db not found — run scripts/load.py first.")
    categories_cfg, targets, txns, accounts_cfg = load_data()
    if not txns:
        raise SystemExit("No transactions in database.")
    d = compute(categories_cfg, targets, txns, accounts_cfg)
    warnings = load_warnings()

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    pages = {
        "index.html": page_dashboard(d, warnings),
        "expenses.html": page_expenses(d),
        "income.html": page_income(d),
        "investments.html": page_investments(d),
        "budget.html": page_budget(d),
        "import.html": page_import(d),
    }
    for name, html in pages.items():
        (SITE_DIR / name).write_text(html)
    print(f"Wrote {len(pages)} pages to {SITE_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
