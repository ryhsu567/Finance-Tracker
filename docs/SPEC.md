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
- **`lumpy`** — a **sinking fund**. The target is a monthly *accrual rate*, not a monthly
  allowance: it adds to a bucket every elapsed month and spending draws the bucket down.
  The figure shown is the balance (`target × months_elapsed − cumulative_spend`), in
  dollars, not a percentage. Flights at $961.53 in February is not a February problem, and
  it isn't "640% of February" either — it's $961.53 out of a bucket that had $300 in it.
  The balance is allowed to go negative: that is the honest statement that spending has
  outrun the accrual cumulatively, which a ratio capped at "over" cannot express. Accrual
  starts at the first month in the ledger and **never resets at a year boundary** — a fund
  has no calendar year (see §11, 2026-11-11).

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
   **Finding, 2026-11-11: a date window cannot do it, and two premises above look wrong.**
   Bookings do not sit inside the trip they pay for — `Korea Flight` is 2026-02-09 for a
   trip whose on-the-ground spending is 2026-04-20/28, `NYC Flights` (2026-10-28) and
   `Taiwan flights` (2026-11-07) are for trips that have not happened yet, and
   `Flight to NYC from SF` (2026-03-25) has no matching cluster at all. So any window
   assigns the booking to one trip and the trip to another, or to none. Relatedly, the
   Feb 12–16 rows grouped as "Korea" above (O2 Valley, Pho Element, Khao Tiew, Malatang,
   PhotoBooth, Clipper, Lyft Ride) read as local Bay Area spending around Valentine's, not
   Korea — the actual Korea trip rows are the April ones (Olive Young, Musinsa, Uniqlo
   Drop, Convenience Stores, Korea Misc.). Needs a per-transaction trip assignment, by
   hand or confirmed row by row; it is not derivable. The Buckets tab lists every charge
   with its date and description instead, so trips stay legible without the build
   asserting a boundary it cannot know.
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
- **Raw descriptions are matched ignoring case.** The description maps
  (`raw_expense_description_labels`, `raw_income_description_labels`) are hand-typed in the
  source sheet, so one payee shows up as `HEYTEA`, `HeyTea` and `Heytea`. Exact matching
  meant a single spelling hit its override and the rest fell through to the `Category`
  column — harmless for HEYTEA, whose raw label already said `Sweet Treat`, but only by
  luck. Folded case closes that (user decision, 2026-10-25) and reclassifies no existing
  row. Two keys differing only in case but disagreeing on the category raise a
  `LoaderError` rather than letting dict order decide (§6.1).
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
- **`travel` is `lumpy` again** (2026-11-10, reversing the 2026-08-11 flip for this one
  category). Travel is saved up for across months and then spent in a lump, so pacing it
  against a flat monthly target misreads it: a single $950 flight booking made November's
  whole discretionary pool read as blown on the 10th, which is the exact failure §5's
  envelope design named ("a trip is one decision, not a monthly overrun"). `travel` now
  gets an annualized YTD meter and leaves the paced pool, which drops the pooled cap from
  $1,450 to $1,300/mo — the pool is the sum of its members' targets, so it moves when
  membership does. `gym` and `shopping` stay `variable`/pooled; the 2026-08-11 preference
  still holds for them. **The $150/mo travel target stays as-is — asked and confirmed with
  the user 2026-11-10**, with the consequence stated: actual travel is $3,973.75 over
  Jan–Oct (~$397/mo), so travel's annualized meter sits at roughly 300% of target and will
  stay red. That is the intended read, not an oversight; the cadence change was about the
  *pool* misattributing one lump purchase, not about making the overrun disappear. Raising
  the target was explicitly declined for now, and would in any case rewrite every past
  month's read until time-versioned targets (§4) reach the read path — the schema has
  `effective_from`, `build_dashboard.py`/`app.js` ignore it (see `config/budgets.yml`).
- **`underlying_spend` is built, re-based on cadence instead of tags** (2026-11-10). §5
  defined it as "expenses excluding all tagged transactions — the trend line that actually
  means something", then §11 descoped tagging, which left the metric defined in terms of an
  axis that no longer exists; it was never implemented. Cadence carries the same meaning:
  a `lumpy` category has no monthly rhythm, so `underlying = expense − lumpy`. Derived per
  month in `compute()`, never stored. The expense trend now plots **total in the de-emphasis
  ink and underlying in the expense accent** — context recessive, the figure you read in the
  accent, the same contract `render_sparkline()` already documented. Total stays on the chart
  deliberately: nothing is hidden, the eye just has a line worth following. Why it was
  needed: travel ran 562 / 1,237 / 139 / 1,319 / 244 / 0 / 135 / 0 / 0 / 337 / 950 across
  Jan–Nov, so the total swung to $5,027 in April and $2,639 in September mostly on flight
  booking dates; ex-travel is a readable 2,423 → 2,755 → 3,726 → 3,708 → 3,136 → 4,363 →
  3,155 → 2,969 → 2,639 → 2,808. With no lumpy category the two lines would coincide, so the
  chart falls back to one accent-colored series and drops the legend. **Amortizing a lump
  across months was considered and rejected**: it invents a figure that never happened on any
  date and would corrupt the net-cash-flow and savings-rate views, which need real monthly
  outflow. A save-up view belongs in its own cumulative chart, not folded into monthly spend.
- **The current month is still plotted as if complete** on the trend charts (asked, declined
  2026-11-10). On 2026-11-10 the November point is $1,531.62 with no rent yet, which reads as
  a collapse in spending rather than a partial month; `render_trend_with_gap_svg()` has no
  notion of a month in progress. Left as-is on purpose — noted so the next person doesn't
  read that dip as real.
- **Node is installed on the dev machine after all.** The "Stack: Python, not Node" entry
  above is still the right call for the project's runtime, but `node` exists at
  `/opt/homebrew/bin/node`, which is what let the dataviz palette validator run for the
  trend-chart colors. Nothing in the build depends on it.
- **`lumpy` is a sinking fund, not an annualized ratio** (user decision 2026-11-11). The
  meter read `ytd` against `target × months_elapsed` and rendered it as a percentage, which
  for travel meant a permanent ~298% and no information. The arithmetic was already right;
  the *shape* was wrong — a ratio can only say "over" or "under", so it cannot express
  saving up and then spending, which is the whole behaviour of a lumpy category. It now
  reports the bucket balance in dollars (`accrued − spent`), with a per-category chart on
  the Budget page plotting that balance over time: it climbs through quiet months and drops
  on the day something is booked. Travel's 2026 line falls on the Feb Korea flight and the
  April trip, climbs Jun–Sep untouched, then drops $337 in October and $950 on the November
  Taiwan booking. Derived in `compute()`/`fund_for()` and mirrored in `app.js`'s
  `categoryResult()`; nothing is stored. The meter bar now fills as the bucket *drains*, so
  a full bar means empty rather than a blown budget — the dollar figure in the note is the
  real read, which is why it leads.
- **The accrual starts at the first month in the ledger, carrying all of 2026** (asked,
  decided 2026-11-11). The alternatives were opening a fresh $0 bucket going forward, or
  seeding an opening balance for what had been saved before January. Carrying the year was
  chosen with the consequence stated: the bucket never goes positive, ending **$3,274
  overdrawn** ($4,923.75 spent against $1,650 accrued over 11 months). It is a deficit
  tracker for 2026 rather than a bucket with anything in it — but the month-to-month
  *slopes* still read correctly, and the overdraw is a true figure, which is the point.
  The $150/mo rate is unchanged, consistent with the 2026-11-10 decision above; at
  $1,800/yr against a ~$448/mo actual, a single $950 booking is 6.3 months of accrual, so
  the fund will go negative again next year unless travel actually slows.
- **Travel still mixes bookings with in-trip daily spend** (noted 2026-11-11, not acted on).
  The 2026 travel rows are partly true lumps (Korea $961.53, Taiwan $950.00, NYC $337.00)
  and partly a second life lived abroad — April's $1,319.33 is Uniqlo, Musinsa, Olive Young,
  a haircut, convenience stores and malatang, i.e. `shopping` and `eating_out` that happened
  on a trip. Trip tagging was descoped (§11), so there is no axis to separate them on. This
  matters for any future attempt to set the travel target from history: that history is
  flights *plus* in-trip living, not a flights budget.
- **The sinking-fund concept, settled in four parts** (user decisions 2026-11-11, talked
  through on concept rather than on the 2026 figures):
  1. **The balance may go negative.** A real bucket cannot hold less than nothing, so this
     is an accounting fiction on purpose: one running number for "travel spending versus
     the travel allowance, cumulatively". The alternative was to floor it at empty and
     report the overflow separately, which is closer to the physical metaphor but yields
     two numbers to read instead of one.
  2. **No calendar years.** The fund carries forward indefinitely. This was already the
     behaviour — `months` is every month in the ledger and the cumulative sum ran across
     all of it — but the variable was named `ytd`, which misdescribed it badly enough to
     invite a future "fix" into a real year-to-date that would have silently emptied every
     fund each January. Renamed `cumulativeSpend`, with a comment saying why.
  3. **Notional, not real money.** The $150/mo does not leave the bank; the fund is a lens
     on existing spending, derived from the config target. If it ever becomes real cash
     moving to savings, the pattern to copy is `emergency_fund` (SPEC §7.4) — a real dated
     transaction, with the balance derived from transactions instead of from config. Worth
     noting the two read identically on the chart but mean different things: one is a
     projection, the other a fact.
  4. **Travel never touches the discretionary pool**, regardless of the fund's balance.
     The considered alternative was to make pool membership conditional — fund-covered
     travel exempt, overflow charged to the pool — which is a sharper idea but is
     incompatible with (1): with no floor at zero there is no overflow, so once the fund is
     underwater *every* travel dollar is uncovered and travel is effectively back in the
     pool, reinstating the exact failure the 2026-11-10 change fixed. So: the pool governs
     month-to-month discretionary spending, the fund governs travel cumulatively. Two
     rhythms, two mechanisms, neither leaking into the other. A negative balance is itself
     the signal; it does not also need to penalise November's groceries.
- **`YTD income` / `YTD expenses` / `YTD net` on the index page were mislabelled** (found
  and fixed 2026-11-11). They summed `monthly_series`, which is every month in the ledger,
  so they were lifetime-cumulative figures wearing a year-to-date label — the same
  confusion `ytd` had in `app.js`. Harmless while the ledger held one calendar year, wrong
  the moment it spanned two. Now scoped to the year of the newest month in the ledger, and
  the card heading names that year. Deliberately *not* the current wall-clock year: the
  dashboard is a view of the ledger, so a January rebuild against December data should show
  that data rather than three empty tiles. The figures are unchanged today ($56,924 /
  $37,245 / $19,680) because the ledger holds only 2026 — this is a correctness fix for the
  first year-boundary crossing, not a change to what is on screen now.
- **Where the bucket is shown** (2026-11-11). Asked whether it deserved its own tab;
  decided no — the nav is data *domains* (Dashboard / Expenses / Income / Investments /
  Budget / Import) and a sinking fund is one category's budget mechanic, so a tab would be
  out of proportion and would split the balance from the chart explaining it. Instead the
  meter and its chart are interleaved inside the single lumpy card on the Budget tab, one
  pair per lumpy category, so they read as one object. The balance also appears as a
  `Spending buckets` tile card on the Dashboard tab — its own card rather than a fourth
  tile under `Year to date`, because a bucket never resets at a year boundary and that card
  is now explicitly scoped to one year. The trigger to revisit the tab question is Fork 3
  flipping (see above): if the accrual ever becomes real cash moving to savings, the
  buckets become real balances in a domain of their own, alongside Investments.
  The chart heading was `accrued vs drawn down` until the user pointed out it was
  accountant-speak for "in vs out" — now `<label> bucket — balance over time`.
- **The buckets get their own tab, named Travel** (user decision 2026-11-11, reversing the
  call made earlier the same day). The earlier reasoning — the nav is data domains, one meter plus
  one chart does not justify a tab — held only while that was all there was. The ask was to
  see accumulated *and* spent, broken down, and accrual, spend, a monthly table and a
  charge list are a domain's worth of content, while the Budget page is about targets. So:
  `buckets.html`, one section per lumpy category, with three tiles (accumulated / spent /
  balance), an accumulated-vs-spent cumulative chart whose vertical gap is the balance, the
  balance line, a month-by-month table (added / spent / running balance), and every charge
  listed newest-first. The Budget page keeps the meter and a one-line state summary linking
  here, rather than a second copy of the chart to keep honest. The Dashboard's `Spending
  buckets` tiles now link here too. Colors reuse `--series-1`/`--series-2`, re-validated as
  a pair for this chart: all six checks pass in both themes (CVD ΔE 24.7 light / 26.8 dark,
  normal-vision 33.6 / 31.8, contrast ≥3:1).
- **No by-trip grouping, and it is not a scoping shortcut** (2026-11-11). Asked for
  "by month or by trip"; by month is derived and shipped, by trip is not possible from the
  data. See §7.1 — bookings precede the trips they pay for by months, two of them are for
  trips that have not happened, so no date window groups them, and inventing a trip
  taxonomy from descriptions is the guessing CLAUDE.md forbids. It needs the
  `transaction_tags`/`envelopes` tables that §11 descoped plus a hand-made trip list. The
  charge list is the stand-in.
- **Named `Travel`, not `Buckets`, and the Dashboard shows bars instead of a number**
  (user request 2026-11-11). The tab is `travel.html` and reads *Travel*, because travel is
  the only lumpy category there is; the page body still loops over every lumpy category
  rather than hard-coding travel, so a second one would appear there automatically and the
  *label* is then the thing to revisit, not the page. On the Dashboard, the single
  "$3,274 overdrawn" tile is replaced by two directly-labelled bars, accumulated against
  spent, scaled to the larger of the two — comparing two magnitudes of the same unit is a
  bar's job, each bar carries its own label so identity never rests on hue alone, and the
  shortfall is the gap between them rather than a figure of its own. The card's caption
  names the accrual rate for the same reason: the request was for the two amounts *instead
  of* the signed number, which is still on the Travel tab where the balance is the subject.
  `render_static_bar()` renders the existing bar-list markup at build time so the static
  card and the JS-driven ones are indistinguishable.
- **New income category `rebate`** (user decision 2026-11-11), for a Costco membership
  rebate. Asked rather than guessed: `reimbursement` is the nearest existing fit but its
  precedent is being paid back for something fronted on someone else's behalf ("Eye Medical
  Reimbursement"), which a rebate is not, and the alternative read was `kind: transfer` on
  the argument that money back on your own groceries is not earnings — counting it as income
  inflates the income and expense totals against each other and flatters the savings rate.
  The call was a category of its own under `kind: income`. No raw-label mapping: rebates
  arrive through the Import tab, not the Income sheet. Adding it meant touching
  `INCOME_LABELS` as well as `categories.yml`, because `income_categories_json` is built
  from that dict's keys — a category missing from it is in the data but renders nowhere on
  the Income page. That dict now says so in a comment.
- **`canonical_categories()` does not check category against kind** (found 2026-11-11, not
  fixed, offered). `scripts/load.py:124` flattens all four kinds into one set, so
  `kind=income` with `category=other` — an expense category — validates clean and lands in
  the database, where it would show up as an income category on the Income page. Nothing in
  the loader or the staging script catches it. This surfaced from a request to file the
  rebate under `other` "for now"; the row was not staged, and a `rebate` category was added
  instead. A kind-aware check belongs in `canonical_categories()` or beside the `VALID_KINDS`
  check, and is squarely in the spirit of §6.1's fail-loudly rule.
