# Finance Tracker

A local dashboard that consolidates my personal budget spreadsheets into one place.

I'd been tracking expenses across a handful of Google Sheets that had drifted out
of sync with each other — different category names, manually copied subtotals,
no single source of truth. This project pulls the raw `Expenses` and `Income`
data into a SQLite database and generates a static dashboard from it, so
everything derives from one place instead of being hand-maintained in five.

## How it works

- **Import** — raw exports land in `data/raw/` (gitignored, it's real transaction
  data) and get parsed by `scripts/xlsx_reader.py` and `scripts/load.py`.
- **Reconcile** — `scripts/reconcile.py` catches duplicates, miscategorized
  transactions, and gaps (e.g. a month with no rent charge) before anything
  gets written to the database.
- **Categorize** — every transaction needs a category defined in
  `config/categories.yml`. If it doesn't match one, the loader fails loudly
  instead of silently dropping it.
- **Budget** — `config/budgets.yml` and `config/envelopes.yml` define spending
  targets per category. Targets carry an `effective_from` date and the schema
  stores them that way, but the dashboard doesn't resolve per-month targets
  yet: it applies one current target to every month, so editing a number
  re-scores past months too. Real time-versioning is still to build.
- **Dashboard** — `scripts/build_dashboard.py` generates the static site in
  `site/`, which shows income, expenses, investment contributions, and
  budget-vs-actual by category.

Nothing is ever stored as a precomputed subtotal — everything on the dashboard
is derived fresh from the transaction-level data.

## Tests

```
python3 -m unittest discover tests
```

Two suites, both stdlib-only: `test_loader_fails_loud.py` pins the
fail-loud contract (unmapped categories, per-kind mismatches, case-ambiguous
config), and `test_fund_parity.py` asserts that the sinking-fund maths in
`build_dashboard.py` and in `site/app.js` still agree month by month — they're
two implementations of the same formula, and nothing else keeps them in step.
The parity suite needs `data/budget.db`, `site/data.js` and `node`, and skips
itself without them.

## Stack

Python (data loading/reconciliation) + SQLite (storage) + a static HTML/JS/CSS
dashboard, no framework.

## Project layout

```
config/   category, budget, and envelope definitions (hand-edited)
scripts/  import, reconciliation, and dashboard-build scripts
site/     the generated dashboard
docs/     schema and design notes
data/     local database + raw imports (gitignored — not in this repo)
```

## Status

Personal tool, actively used for my own budget. Not set up for anyone else's
data out of the box — categories, budgets, and the import format are all
specific to how I track my own spending.
