# Budget Dashboard — Project Spec (v2)

Redesign of an existing spreadsheet system. The concepts in the source sheets are sound;
the structure underneath them is not. This spec keeps the concepts and rebuilds the structure.

Drop in the repo as `docs/SPEC.md`. Keep `CLAUDE.md` short and point at this file.

---

## 1. What's being replaced

| Source | Role going forward |
|---|---|
| `Expenses` tab | **Source of truth** — transaction ledger |
| `Income` tab | **Source of truth** — transaction ledger |
| `Finances Summary` tab | **Derived** — regenerate, never import |
| Monthly tracker cards | **Derived**, except the Investments column |
| Budget targets sheet | **Config** → `config/budgets.yml` |
| `Food`, `Travel Diaries`, `2.2-2.8`, `1.26-2.1` tabs | Unknown — see §7 |

The existing chain reconciles correctly (July: ledger → Summary `Jul-26` → Baseline
$2,490.99 → Total $3,437.54). This is a migration, not a rescue. Preserve that arithmetic
as a test, then improve on it.

---

## 2. Core design change: three axes, not one column

The source `Category` column encodes three unrelated facts at once. Split them.

**`kind`** — does this change net worth?
`expense` · `income` · `transfer` · `investment`

Fixes: June income currently reads $9,894 because it includes a $1,801.25 security
deposit return and $3,000 of "Stock Money." Those are asset movements. Counting them as
income makes every savings-rate figure meaningless.

**`category`** — what was it, in merchant terms?
Two-level: `group` → `category`. Always the true category, regardless of context.

**`tags`** — why did it happen?
`trip:sf_2026_01`, `event:move_2026_06`. Orthogonal, optional, many-per-transaction.

Consequence: **`travel` and `move_in` stop being categories.** A CVS run during a trip is
`personal` + `trip:sf_2026_01`. Furniture during the move is `shopping` + `event:move_2026_06`.
You get both readings — trip totals and honest category trends — instead of choosing.

---

## 3. Category taxonomy

| Group | Categories | Budget group | Cadence |
|---|---|---|---|
| `housing` | `rent` | baseline | fixed |
| `housing` | `utilities`, `insurance` | baseline | variable |
| `food_in` | `groceries` | discretionary | variable |
| `transport` | `transportation` | discretionary | variable |
| `transport` | `flights` | discretionary | lumpy |
| `food_out` | `eating_out`, `sweet_treat` | discretionary | variable |
| `social` | `going_out` | discretionary | variable |
| `health` | `gym` | discretionary | fixed |
| `personal` | `shopping` | discretionary | lumpy |
| `personal` | `other` | discretionary | variable |

**Cadence drives which progress metric is shown** (see §5). This is the fix for the
percentage problem — correcting the denominators alone isn't enough, because one metric
can't describe rent and flights at the same time.

### Raw label mapping

Expenses: `Rent`→`rent` · `Groceries`→`groceries` · `Utilities`→`utilities` ·
`Transportation`→`transportation` · `Insurance`→`insurance` · `Eating Out`→`eating_out` ·
**`Food`→`eating_out`** · `Sweet Treat`→`sweet_treat` · `Going Out`→`going_out` ·
`Flights`→`flights` · `Gym`→`gym` · `Shopping`→`shopping` · `Other`→`other` ·
`Travel`→ **needs per-row review**, becomes real category + trip tag ·
`Move-in`→ **needs per-row review**, becomes real category + event tag

Income: `Deloitte`→`salary` · `Holidays`→`gift` · `Side Hustle?`→`side_hustle` ·
`Tax Refund - Federal/State`→`tax_refund` · `Eye Medical Reimbursement`→`reimbursement` ·
`FB Marketplace BS`, `Underdog`→`resale` ·
`Stock Money`→`stock_sale` (**kind=transfer**) · `Security Deposit`→`deposit_return` (**kind=transfer**)

---

## 4. Schema

```sql
CREATE TABLE transactions (
  id            TEXT PRIMARY KEY,   -- hash(source_file, sheet, row)
  date          DATE NOT NULL,
  description   TEXT NOT NULL,
  amount        DECIMAL NOT NULL,   -- always positive; direction lives in kind
  kind          TEXT NOT NULL,      -- expense | income | transfer | investment
  category      TEXT NOT NULL,      -- canonical slug
  note          TEXT,
  source_file   TEXT NOT NULL,
  source_sheet  TEXT NOT NULL,
  source_row    INTEGER NOT NULL
);

CREATE TABLE transaction_tags (
  transaction_id TEXT NOT NULL REFERENCES transactions(id),
  tag            TEXT NOT NULL,     -- 'trip:sf_2026_01', 'event:move_2026_06'
  PRIMARY KEY (transaction_id, tag)
);

CREATE TABLE envelopes (              -- named spend containers with their own budget
  tag            TEXT PRIMARY KEY,    -- matches transaction_tags.tag
  label          TEXT NOT NULL,       -- 'SF trip, January'
  budget         DECIMAL,             -- nullable
  start_date     DATE,
  end_date       DATE
);

CREATE TABLE budgets (
  scenario       TEXT NOT NULL,       -- 'current' | 'proposed'
  category       TEXT NOT NULL,
  monthly_target DECIMAL NOT NULL,
  effective_from DATE NOT NULL,       -- time-versioned; see below
  PRIMARY KEY (scenario, category, effective_from)
);
```

**Time-versioned budgets matter here.** Rent went $1,844 → $1,800 in May while the target
still says $1,900. Without `effective_from`, correcting a target silently rewrites history.

**Never store a subtotal.** Baseline, Other Expenses, and all totals are derived. The source
sheet's `Other Expenses` row is wrong in both scenarios precisely because it was stored:
monthly rows sum to $1,030 against a stored $980, yearly rows sum to $12,360 against $3,600.

**Drop `Month-Year`.** Derived from `date`, stored as text (`1-2026`), one more thing to
drift out of sync.

---

## 5. Metrics

### Progress by cadence

- **`fixed`** — binary. "Rent: paid, $1,800 (target $1,900)". No percentage bar; a
  percentage on a once-monthly charge communicates nothing useful.
- **`variable`** — pace-adjusted. Compare month-to-date against expected-to-date
  (`target × day_of_month / days_in_month`). Being at 60% of budget on the 12th is over
  pace; on the 25th it's under. A raw percentage can't tell you which.
- **`lumpy`** — annualized. Compare trailing 12 months against `target × 12`. Flights at
  $961.53 in February is not a February problem; it's a $1,426 YTD figure against a
  $1,200 annual target.

### Envelope pooling

Baseline is checked per-category. **Discretionary is checked as a pool.** Per-category
targets stay visible as guides, but the constraint is the total. In July, Gym at 4.64%
and Eating Out at 124.62% are noise; $946.55 against $1,030 is the signal.

Trip and event envelopes are budgeted separately and **excluded from the monthly
discretionary pool**. A trip is one decision, not a monthly overrun.

### Headline figures

- `net_cash_flow` = income − expenses, transfers excluded
- `savings_rate` = net_cash_flow / income
- `underlying_spend` = expenses excluding all tagged transactions — the trend line that
  actually means something
- `investment_contributions` tracked separately. Flag when contributions exceed net cash
  flow: July shows $10,600 invested against $4,553.97 income, which means drawing down
  cash, not saving from income. The current sheets can't show this at all.
- **Rolling 3-month median**, not mean. Gym went $298 → $13; the mean is a lie either way.

### Annualization

`monthly = yearly / 12` everywhere. The source income block divides by 13 (89,000 → 6,846;
26,700 → 2,054; 56,960 → 4,382) while Total Investments multiplies by 12. If the 13 is
deliberate (biweekly pay periods), make it a named constant in `budgets.yml` with a
comment — don't bury it in a divisor.

---

## 6. Integrity checks — run on every load, fail the build

1. **Unmapped category → hard fail.** `Expenses` row 43 (`2/15/26, Lunch w/ Shashank, $40.00, Food`)
   is silently dropped by the current summary. Proof: Feb `Eating Out` = 104.39 = exactly
   the five Eating Out rows; Feb `Other` = 29.92 = exactly the two Other rows; the Feb
   total is short by exactly $40. One typo, one vanished transaction, no error anywhere.
   This is the bug the project exists to prevent.
2. **Duplicate detection** — same date + amount + description, warn.
3. **Month coverage gaps** — a month with zero rent is almost certainly missing data.
4. **Budget denominator test** — assert each category's target resolves to its own row.
   The source tracker divides `Going Out`, `Move-in`, and `Shopping` by the **Gym** budget
   of $280 (Jul Shopping $138.14 → 49.34% = 138.14/280; actual is 138%). A formula drag
   misaligned when Flights left the tracker and Move-in joined.
5. **Migration reconciliation** — reproduce `Finances Summary` totals for Jan–Aug, with
   the Feb $40 as a documented known exception. This is the proof the rebuild is safe.

---

## 7. Open questions

1. **Trip boundaries.** Which rows belong to which trip? January has an SF cluster
   (Flights to SF, Uber+Lyft for SF, Bi-Rite Market, CVS, Sausalito Ferry, Ben Thai);
   February/March have Korea (Korea Flight, O2 Valley, Pho Element, Khao Tiew, Malatang).
   Confirm the date ranges so `trip:` tags can be assigned by date window rather than by hand.
2. **Which budget scenario is live?** The tracker uses Scenario A (Rent $1,900, Gym $280,
   no Insurance). Scenario B is a different shape — Rent $2,000, adds Insurance, 401k Roth,
   Entertainment; drops Flights, Gym, Travel. Proposal, post-move plan, or dead?
3. **The unknown tabs** — do `Food`, `Travel Diaries`, `2.2-2.8`, `1.26-2.1` contain
   transactions not in the main ledger, or are they notes? If transactions, they're in scope.
4. ~~**Investment ledger.** Brokerage contributions ($10,600 July, $5,000 June) exist only in
   the tracker cards. Need dates and amounts as real rows, or there's no history to chart.
   Also: is the 70/20/10 brokerage/emergency/crypto split a target or a description?~~
   Resolved (§11): real dated rows, `kind=investment`, three accounts, 70/20/10 descriptive.
5. **File inventory** — the tracker cards and budget sheet look like a separate workbook
   from Expenses/Income/Summary. Confirm paths.
6. ~~**Gym** — $280/mo budgeted, actuals swing $298 → $13. Membership plus classes, or lumpy?~~
   Resolved (§11): `lumpy`.

---

## 8. Build order

1. **Inspect only, no code.** Enumerate every distinct raw category, date format, and tab;
   diff against §3; answer what §7 the data can answer.
2. **Config files** — `config/categories.yml`, `config/budgets.yml`, `config/envelopes.yml`.
   Hand-editable. These are what gets touched monthly, not code.
3. **Loader → SQLite.** Idempotent, provenance-recording, fails on unmapped categories.
4. **Reconciliation test** (§6.5) before any UI work.
5. **Metrics layer** — pure functions over the ledger, unit-tested. Not in the view.
6. **Static dashboard.** Build step emits JSON; site reads only JSON.
7. **Monthly loop** — drop export in `data/raw/` → `python scripts/load.py`.

Dates parse as `M/D/YY` with an explicit format string. Never let a date library guess.
Never commit `data/raw/`.

---

## 9. Dashboard views

> **Descoped (see decisions log below):** `travel` and `move_in` stay as plain categories,
> not category+tag. No `trip:`/`event:` tags, no per-row recategorization. The **Trend**
> shading and **Envelope view** below assumed tags existed — both are cut until/unless
> tagging comes back in scope.

- **This month** — net cash flow, savings rate, baseline status (per-category, cadence-aware),
  discretionary pool remaining
- **Trend** — actual vs underlying monthly spend
- **Budget vs actual** — correct denominators, pace-adjusted where the cadence warrants it
- **Category drilldown** — click a category-month, see the transactions
- **Cash flow vs investing** — income → expenses → contributions, with drawdown flagged

---

## 10. Ingestion & automation (stretch goal)

§8 step 7's "monthly loop" as written is manual: drop an export in `data/raw/`, run a build
command from a terminal. The actual target is more automated than that — an upload flow
inside the dashboard itself (spreadsheet exports, and ideally screenshots) that parses new
data and refreshes the dashboard without touching the file system or a terminal.

This needs a running backend (or serverless functions) to handle parsing on upload, not just
a static-JSON build step — a bigger architecture than §9's "build step emits JSON; site reads
only JSON." Design it once the loader (§8 step 3) exists and its parsing logic can be reused
behind an upload endpoint, rather than duplicated between a CLI path and a web path.

---

## 11. Decisions log

- **Travel stays a category.** No `trip:`/`event:` tagging, no per-row recategorization of
  `Travel`/`Move-in` rows. Simpler schema than §2 originally proposed; §9's tag-dependent
  views are cut accordingly.
- **Scenario A is live.** Scenario B (Rent $2,000, adds Insurance/401k Roth/Entertainment,
  drops Flights/Gym/Travel) is unreferenced by any formula in the source workbook — treat as
  dead unless told otherwise.
- **The four unknown tabs are out of scope.** `Food`, `Travel Diaries`, `2.2-2.8`, `1.26-2.1`
  don't exist in the files currently in `data/raw/` and aren't being chased down.
- **Investment ledger**: a fuller record exists outside `data/raw/` (a screenshot) — not yet
  pulled in. Until it is, `config/budgets.yml`/the loader treat monthly lump-sum
  Brokerage/Roth/Crypto totals (no per-transaction dates) as the available granularity.
- **Stack: Python, not Node.** Node/npm aren't installed on the dev machine; only Python 3
  is available. `scripts/load.py` replaces the `npm run build` language in §8/§9 — same
  shape (drop export → run one command → JSON out), different runtime.
- **Category taxonomy: 11, not 14.** Merged three sparse/inconsistent categories into a
  same-cadence sibling: `insurance`→`utilities`, `flights`→`travel`, `move_in`→`shopping`.
  `sweet_treat` stays separate from `eating_out` (an earlier attempt to merge it was
  reverted). `going_out` renamed `entertainment` and widened to catch recreational spend
  (Bay to Breakers, Movies, Vegas Gambling, tennis, Running Gels) that the raw sheet filed
  under the same `Other` label as fees/subscriptions/personal care — split by transaction
  description, same mechanism as Income's `Other` bucket. See `config/categories.yml` for
  the full raw-label mapping.
- **Gym cadence: `lumpy`, not `fixed`.** Resolves §7.6. Actuals swing $298 → $13 → $17
  month to month; a binary paid/not-paid read was never meaningful here.
- **Discretionary pool excludes lumpy categories.** Only variable-cadence categories
  (`eating_out`, `sweet_treat`, `entertainment`, `other`) are pooled and paced against
  day-of-month. `travel`/`shopping`/`gym` (all `lumpy`) are tracked individually against
  their own annualized YTD target instead — pooling them made one early-month lumpy
  purchase read as the whole pool over pace, which is what §5's original envelope design
  ("a trip is one decision, not a monthly overrun") was already trying to prevent.
- **Budget groups and cadences were revised after the bullets above.** Superseding the
  two previous entries: no discretionary category is `lumpy` any more — `gym`, `shopping`
  and `travel` were all flipped to `variable` and pooled on 2026-08-11 (user preference: a
  flat monthly pace, not an annualized YTD read), so the Budget page's "lumpy" section
  hides itself. Baseline shrank twice, and is now just `rent` + `utilities`: `groceries`
  moved to the discretionary pool on 2026-08-11 and `transportation` on 2026-10-23
  (ride-share/transit/parking are day-to-day choices, so they belong in the pooled check).
  The pooled target is therefore $1,450/mo, re-confirmed with the user on 2026-10-25 as the
  real cap — it supersedes the $1,000/mo cap of 2026-08-11, which the two moves into the
  pool had quietly broken. The per-category targets in `config/budgets.yml` were left
  unchanged rather than cut back to fit the older figure.
  `config/categories.yml` is the source of truth for both axes; `reconcile.py`'s
  `BASELINE_CATS` deliberately keeps the *source sheet's* historical baseline (rent,
  groceries, utilities, transportation) because it validates the migration arithmetic
  against §5's worked example, and is not a statement about current grouping.
- **`fixed` cadence checks amount, not just presence.** A payment under 95% of target now
  reads "partial: $X of $Y" (`warning`), not indistinguishable from a full on-target payment.
- **Investment contributions are real dated rows** (resolves §7.4). `kind=investment`,
  three accounts in `config/categories.yml` — `brokerage`, `emergency_fund`, `crypto` —
  entered through the Import tab, not transcribed into config as monthly lump sums (the
  earlier fallback in this log). Monthly totals are derived like every other figure on the
  site. The tracker cards in `data/raw/2026 Budget.xlsx` turned out to hold no investment
  numbers at all (all 12 month cards export as zeros), so there was nothing to import even
  if the hard rule allowed it. **70/20/10 is descriptive, not a target** — the Investments
  tab charts the actual mix and draws no target line. **401k/Roth payroll contributions are
  out of scope**: they never appear as cash transactions, so including them would corrupt
  the contributions-vs-net-cash-flow comparison that §5 asks for.
- **An unhandled `kind` now fails the dashboard build.** `compute()` silently ignored any
  kind without a branch, which would have dropped every investment row from every view —
  SPEC §6.1's failure mode one layer downstream from the loader.
- **The current month can't be a rent coverage gap** (§6.3). Rent is paid at the end of the
  month, so the check fired every month until payday and surfaced as a dashboard banner.
  Months that have ended still warn.
- **Loader warnings surface on the Dashboard**, not just the terminal. `scripts/load.py`
  writes `data/load_warnings.json` every run; `build_dashboard.py` reads it and renders a
  banner when non-empty — a duplicate transaction or a month missing rent no longer sits
  invisible unless someone happens to be reading `load.py`'s stdout.
