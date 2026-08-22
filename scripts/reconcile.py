#!/usr/bin/env python3
"""Reconciliation test (SPEC §6.5 / §8 step 4).

Proves the migration is safe by reproducing `Finances Summary`'s Jan-Aug
totals two ways:

  A. "old method" - group Expenses/Income rows by their literal raw
     Category text, the way the source sheet's own formulas presumably did.
     This must match Finances Summary exactly. If it doesn't, our row
     extraction is wrong - not the source sheet.
  B/C. "canonical" - group by the mapped category from config/categories.yml
     (read straight from data/budget.db - what the loader actually
     produced). Expected to differ from Finances Summary only where we know
     why: rows the old sheet silently dropped (Food/Sho/Gambling, now
     folded into a real category) and income that's actually a transfer
     (Stock Money/Security Deposit) pulled out of the income total.

Any OTHER discrepancy is a real bug and fails the script (nonzero exit).
Run after scripts/load.py, before any UI work (SPEC §8 step 4).
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_reader import Workbook, excel_serial_to_date

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "raw" / "Gamify copy.xlsx"
DB_PATH = ROOT / "data" / "budget.db"

MONTHS = [f"{i}-2026" for i in range(1, 9)]  # Jan..Aug 2026 - the only months with data
TOL = 0.01

EXPENSE_ROW_LABELS = {
    5: "Rent", 6: "Groceries", 7: "Utilities", 8: "Transportation", 9: "Insurance",
    11: "Eating Out", 12: "Flights", 13: "Gym", 14: "Going Out", 15: "Move-in",
    16: "Shopping", 17: "Other", 18: "Travel", 19: "Sweet Treat",
}
EXPENSE_TOTAL_ROW = 22
INCOME_ROW_LABELS = {38: "Deloitte", 39: "Holidays", 40: "Side Hustle?", 41: "Other"}
INCOME_TOTAL_ROW = 43

def month_key(d):
    return f"{d.month}-{d.year}"


def read_finances_summary():
    wb = Workbook(LEDGER)
    rows = dict(wb.read_sheet("Finances Summary"))
    wb.close()
    expense = {label: {m: float(rows[r].get(m, 0) or 0) for m in MONTHS}
               for r, label in EXPENSE_ROW_LABELS.items()}
    expense_total = {m: float(rows[EXPENSE_TOTAL_ROW].get(m, 0) or 0) for m in MONTHS}
    income = {label: {m: float(rows[r].get(m, 0) or 0) for m in MONTHS}
              for r, label in INCOME_ROW_LABELS.items()}
    income_total = {m: float(rows[INCOME_TOTAL_ROW].get(m, 0) or 0) for m in MONTHS}
    return expense, expense_total, income, income_total


def read_raw_ledger():
    wb = Workbook(LEDGER)
    expense_rows, income_rows = [], []
    for _, record in wb.read_sheet("Expenses"):
        d = excel_serial_to_date(record.get("Date"))
        if d is None:
            continue
        expense_rows.append((month_key(d), record.get("Category"),
                             record.get("Expense") or "", float(record.get("Amount", 0))))
    for _, record in wb.read_sheet("Income"):
        d = excel_serial_to_date(record.get("Date"))
        if d is None:
            continue
        income_rows.append((month_key(d), record.get("Category"),
                             record.get("Income") or "", float(record.get("Amount", 0))))
    wb.close()
    return expense_rows, income_rows


def old_method_totals(rows, known_labels, cat_index):
    """Group by literal raw Category text; anything not in known_labels is
    what the old sheet's formulas would have silently missed."""
    totals = defaultdict(lambda: defaultdict(float))
    dropped = []
    for row in rows:
        month, raw_cat = row[0], row[cat_index]
        if raw_cat in known_labels:
            totals[raw_cat][month] += row[-1]
        else:
            dropped.append(row)
    return totals, dropped


def canonical_totals_from_db():
    conn = sqlite3.connect(DB_PATH)
    expense = defaultdict(lambda: defaultdict(float))
    income_kind_total = defaultdict(float)
    transfers = []
    for date, category, amount, kind, desc in conn.execute(
        "SELECT date, category, amount, kind, description FROM transactions"
    ):
        y, m, _ = date.split("-")
        month = f"{int(m)}-{y}"
        if kind == "expense":
            expense[category][month] += amount
        elif kind == "income":
            income_kind_total[month] += amount
        elif kind == "transfer":
            transfers.append((month, category, desc, amount))
    conn.close()
    return expense, income_kind_total, transfers


def main():
    if not DB_PATH.exists():
        print("data/budget.db not found - run scripts/load.py first.", file=sys.stderr)
        sys.exit(1)

    fs_expense, fs_expense_total, fs_income, fs_income_total = read_finances_summary()
    expense_rows, income_rows = read_raw_ledger()
    old_expense, dropped_expense = old_method_totals(
        expense_rows, set(EXPENSE_ROW_LABELS.values()), cat_index=1)
    old_income, dropped_income = old_method_totals(
        income_rows, set(INCOME_ROW_LABELS.values()), cat_index=1)
    canon_expense, canon_income_by_month, transfers = canonical_totals_from_db()
    categories_cfg = yaml.safe_load((ROOT / "config" / "categories.yml").read_text())
    # Derived from config, not hardcoded - a stale hardcoded copy of this
    # mapping is exactly the kind of drift this test exists to catch.
    raw_to_canon = {rl: categories_cfg["raw_expense_labels"][rl] for rl in EXPENSE_ROW_LABELS.values()}

    failures = []

    print("=== A: old-method reproduction vs Finances Summary (must match exactly) ===")
    mismatches = []
    for label, months in fs_expense.items():
        for m, expected in months.items():
            got = old_expense[label][m]
            if abs(got - expected) > TOL:
                mismatches.append(f"expense {label} {m}: sheet={expected} recomputed={got}")
    for label, months in fs_income.items():
        for m, expected in months.items():
            got = old_income[label][m]
            if abs(got - expected) > TOL:
                mismatches.append(f"income {label} {m}: sheet={expected} recomputed={got}")
    if mismatches:
        for msg in mismatches:
            print(f"  FAIL: {msg}")
        failures.extend(mismatches)
    else:
        print("  PASS - every raw-label bucket reproduces Finances Summary exactly.")

    print("\n=== B: rows the old sheet silently dropped (raw category matched nothing) ===")
    if not dropped_expense and not dropped_income:
        print("  (none)")
    for month, raw_cat, desc, amount in dropped_expense:
        print(f"  expense  {month:8s} {raw_cat!r:12s} {desc!r:28s} ${amount:.2f}")
    for month, raw_cat, desc, amount in dropped_income:
        print(f"  income   {month:8s} {raw_cat!r:12s} {desc!r:28s} ${amount:.2f}")

    print("\n=== C: canonical vs Finances Summary - every difference must be explained ===")
    # Several raw Finances-Summary rows can now fold into one canonical
    # category (SPEC §11, e.g. Insurance+Utilities -> utilities) - group by
    # canonical target and sum, rather than comparing 1:1.
    expected_delta = defaultdict(float)
    for month, raw_cat, desc, amount in dropped_expense:
        canon = categories_cfg["raw_expense_labels"].get(raw_cat)
        if canon:
            expected_delta[(month, canon)] += amount

    # Description-based reclassification within "Other" (SPEC §11) - e.g.
    # "Bay to Breakers" naively maps to `other` but the loader sends it to
    # `entertainment`. Both sides of that move need to show up as expected.
    desc_overrides = categories_cfg.get("raw_expense_description_labels", {})
    for month, raw_cat, desc, amount in expense_rows:
        if raw_cat == "Other" and desc in desc_overrides:
            naive_canon = categories_cfg["raw_expense_labels"].get(raw_cat)
            real_canon = desc_overrides[desc]
            if real_canon != naive_canon:
                expected_delta[(month, naive_canon)] -= amount
                expected_delta[(month, real_canon)] += amount

    canon_to_raw_labels = defaultdict(list)
    for raw_label in EXPENSE_ROW_LABELS.values():
        canon = categories_cfg["raw_expense_labels"].get(raw_label)
        if canon:
            canon_to_raw_labels[canon].append(raw_label)

    unexplained = []
    for canon, raw_labels in sorted(canon_to_raw_labels.items()):
        for m in MONTHS:
            old_val = sum(fs_expense[rl][m] for rl in raw_labels)
            new_val = canon_expense[canon][m]
            expected_diff = expected_delta.get((m, canon), 0.0)
            actual_diff = new_val - old_val
            if abs(actual_diff - expected_diff) > TOL:
                unexplained.append(
                    f"{canon} {m}: sheet={old_val:.2f} ({'+'.join(raw_labels)}) canonical={new_val:.2f} "
                    f"diff={actual_diff:.2f} expected={expected_diff:.2f}")
            elif abs(actual_diff) > TOL:
                print(f"  explained: {canon} {m}: {old_val:.2f} -> {new_val:.2f} "
                      f"(+{actual_diff:.2f}, from {'+'.join(raw_labels)} folding in / reclassified rows)")
    if unexplained:
        for u in unexplained:
            print(f"  FAIL (unexplained): {u}")
        failures.extend(unexplained)
    else:
        print("  No unexplained differences.")

    print("\n=== D: income total, transfers pulled out (Stock Money / Security Deposit) ===")
    for m in MONTHS:
        old_total = fs_income_total[m]
        new_total = canon_income_by_month[m]
        month_transfers = [t for t in transfers if t[0] == m]
        transfer_sum = sum(t[3] for t in month_transfers)
        expected = old_total - transfer_sum
        ok = abs(new_total - expected) <= TOL
        print(f"  {m:8s} sheet_total={old_total:9.2f}  transfers_removed={transfer_sum:8.2f}  "
              f"canonical_income={new_total:9.2f}  [{'OK' if ok else 'FAIL'}]")
        if not ok:
            failures.append(f"income total mismatch {m}: expected {expected}, got {new_total}")

    print("\n=== E: spec's worked example (July), adjusted for known reclassifications ===")
    july_reclass = sum(v for (m, c), v in expected_delta.items() if m == "7-2026")
    july_baseline = sum(canon_expense[c]["7-2026"] for c in ("rent", "groceries", "utilities", "transportation"))
    july_total_expense = sum(canon_expense[c]["7-2026"] for c in set(raw_to_canon.values()))
    expected_july_total = 3437.54 + july_reclass
    print(f"  July Baseline:       {july_baseline:.2f}  (spec: $2,490.99, unaffected by reclassification)")
    print(f"  July Total Expenses: {july_total_expense:.2f}  "
          f"(spec: $3,437.54 + ${july_reclass:.2f} reclassified = ${expected_july_total:.2f})")
    if abs(july_baseline - 2490.99) > TOL:
        failures.append(f"July baseline mismatch: got {july_baseline}")
    if abs(july_total_expense - expected_july_total) > TOL:
        failures.append(f"July total expense mismatch: got {july_total_expense}, expected {expected_july_total}")

    print("\n" + "=" * 70)
    if failures:
        print(f"RECONCILIATION FAILED - {len(failures)} unexplained issue(s).")
        sys.exit(1)
    print("RECONCILIATION PASSED - migration is safe.")


if __name__ == "__main__":
    main()
