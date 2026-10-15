#!/usr/bin/env python3
"""The monthly loader (SPEC §8 step 3 / §8 step 7).

    python scripts/load.py

Scans data/raw/*.xlsx for workbooks containing Expenses/Income sheets,
validates every row's category against config/categories.yml, and rebuilds
data/budget.db from scratch. Idempotent: transaction ids are a hash of
(source_file, sheet, row), so re-running against the same exports produces
the same rows, not duplicates.

Fails loudly (nonzero exit, no partial DB left in a half-written state) on
any category not in config/categories.yml — SPEC §6.1, the exact bug this
project exists to prevent. Duplicate transactions and months missing rent
are warned about, not failed, per SPEC §6.2/§6.3.
"""
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_reader import Workbook, excel_serial_to_date

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "budget.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
WARNINGS_PATH = ROOT / "data" / "load_warnings.json"
IMPORTS_APPROVED = ROOT / "data" / "imports" / "approved.jsonl"
CORRECTIONS_PATH = ROOT / "data" / "corrections.yml"
VALID_KINDS = {"expense", "income", "transfer", "investment"}


class LoaderError(Exception):
    """An unrecoverable data problem — the load must stop, not limp on."""


def load_config():
    categories = yaml.safe_load((CONFIG_DIR / "categories.yml").read_text())
    budgets = yaml.safe_load((CONFIG_DIR / "budgets.yml").read_text())
    envelopes = yaml.safe_load((CONFIG_DIR / "envelopes.yml").read_text())
    return categories, budgets, envelopes


def transaction_id(source_file, source_sheet, source_row):
    key = f"{source_file}|{source_sheet}|{source_row}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def map_expense_category(raw_category, description, categories_cfg, *, sheet, row, source_file):
    mapping = categories_cfg["raw_expense_labels"]
    desc_overrides = categories_cfg.get("raw_expense_description_labels", {})
    # Only "Other" rows get the description-based split (SPEC §11) — the
    # source sheet's Category column can't otherwise tell a fee/subscription
    # apart from a Movies ticket, both filed under "Other".
    if raw_category == "Other" and description in desc_overrides:
        return desc_overrides[description], "expense"
    category = mapping.get(raw_category)
    if category is None:
        raise LoaderError(
            f"Unmapped Expenses category {raw_category!r} "
            f"({source_file}, sheet {sheet!r}, row {row}). "
            f"Add it to config/categories.yml under raw_expense_labels — "
            f"do not guess, this is exactly the bug SPEC §6.1 exists to catch."
        )
    return category, "expense"


def map_income_category(raw_category, description, categories_cfg, *, sheet, row, source_file):
    by_category = categories_cfg["raw_income_category_labels"]
    by_description = categories_cfg["raw_income_description_labels"]

    category = by_category.get(raw_category)
    if category is None:
        category = by_description.get(description)
    if category is None:
        raise LoaderError(
            f"Unmapped Income row: category={raw_category!r} description={description!r} "
            f"({source_file}, sheet {sheet!r}, row {row}). "
            f"Add it to config/categories.yml under raw_income_category_labels or "
            f"raw_income_description_labels — do not guess."
        )
    kind = "transfer" if category in ("stock_sale", "deposit_return") else "income"
    return category, kind


def canonical_categories(categories_cfg):
    """Flatten config/categories.yml's nested taxonomy into the flat set of
    valid canonical category slugs — expense groups are nested by budget
    group, income/transfer are not. Used to validate already-mapped rows
    (Import tab approvals) — distinct from the raw-label maps in
    map_expense_category/map_income_category, which translate *raw* sheet
    labels into these same canonical slugs."""
    cats = set()
    for group in categories_cfg["expense"].values():
        cats.update(group.keys())
    cats.update(categories_cfg["income"].keys())
    cats.update(categories_cfg["transfer"].keys())
    cats.update(categories_cfg.get("investment", {}).keys())
    return cats


def load_approved_imports(categories_cfg):
    """SPEC §10 stretch goal, scoped down: the Import tab's approved queue
    (data/imports/approved.jsonl, one JSON object per line, append-only).
    These rows already carry a canonical category (assigned at
    staging/approval time, not a raw sheet label) — re-validate against the
    taxonomy anyway, same fail-loud rule as SPEC §6.1, since a hand-edited
    or bug-produced entry could still carry a typo'd category."""
    if not IMPORTS_APPROVED.exists():
        return []
    valid_categories = canonical_categories(categories_cfg)
    transactions = []
    for row_num, line in enumerate(IMPORTS_APPROVED.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row["kind"] not in VALID_KINDS:
            raise LoaderError(
                f"Import row {row_num} has invalid kind {row['kind']!r} "
                f"(approved.jsonl) — must be one of {sorted(VALID_KINDS)}."
            )
        if row["category"] not in valid_categories:
            raise LoaderError(
                f"Import row {row_num} has unmapped category {row['category']!r} "
                f"(approved.jsonl, {row.get('description')!r}). "
                f"Add it to config/categories.yml first — do not guess."
            )
        transactions.append(dict(
            id=row.get("id") or transaction_id("import", "approved", row_num),
            date=row["date"],
            description=row["description"],
            amount=abs(float(row["amount"])),
            kind=row["kind"],
            category=row["category"],
            note=row.get("note"),
            source_file=row.get("source_file", "import"),
            source_sheet="import",
            source_row=row_num,
        ))
    return transactions


def find_ledger_workbooks():
    """Every .xlsx in data/raw/ that has an Expenses and/or Income sheet.
    Workbooks with neither (e.g. the tracker/config workbook) are skipped —
    only Expenses/Income are source of truth, SPEC §1."""
    found = []
    for path in sorted(DATA_RAW.glob("*.xlsx")):
        wb = Workbook(path)
        names = set(wb.sheet_names())
        if "Expenses" in names or "Income" in names:
            found.append((path, wb))
        else:
            wb.close()
            print(f"  skipping {path.name} — no Expenses/Income sheet ({sorted(names)})")
    return found


def extract_rows(wb, sheet_name, path):
    """Rows with a parseable Date, keyed by (row_num, date, record dict)."""
    for row_num, record in wb.read_sheet(sheet_name):
        d = excel_serial_to_date(record.get("Date"))
        if d is None:
            continue  # blank/formatting-only row, not real data
        yield row_num, d, record


def build_transactions(categories_cfg):
    transactions = []
    for path, wb in find_ledger_workbooks():
        with wb:
            if "Expenses" in wb.sheet_names():
                for row_num, d, record in extract_rows(wb, "Expenses", path):
                    raw_category = record.get("Category")
                    description = record.get("Expense") or ""
                    category, kind = map_expense_category(
                        raw_category, description, categories_cfg,
                        sheet="Expenses", row=row_num, source_file=path.name,
                    )
                    transactions.append(dict(
                        id=transaction_id(path.name, "Expenses", row_num),
                        date=d.isoformat(),
                        description=record.get("Expense") or "",
                        amount=abs(float(record.get("Amount", 0))),
                        kind=kind,
                        category=category,
                        note=record.get("Note"),
                        source_file=path.name,
                        source_sheet="Expenses",
                        source_row=row_num,
                    ))
            if "Income" in wb.sheet_names():
                for row_num, d, record in extract_rows(wb, "Income", path):
                    raw_category = record.get("Category")
                    description = record.get("Income") or ""
                    category, kind = map_income_category(
                        raw_category, description, categories_cfg,
                        sheet="Income", row=row_num, source_file=path.name,
                    )
                    transactions.append(dict(
                        id=transaction_id(path.name, "Income", row_num),
                        date=d.isoformat(),
                        description=description,
                        amount=abs(float(record.get("Amount", 0))),
                        kind=kind,
                        category=category,
                        note=record.get("Note"),
                        source_file=path.name,
                        source_sheet="Income",
                        source_row=row_num,
                    ))
    return transactions


def load_corrections():
    """Hand-edited fixes for a row that's wrong in the source spreadsheet
    itself (a duplicate entry, a charge that was later refunded) —
    data/corrections.yml, keyed by transaction id (the stable
    hash(source_file, source_sheet, source_row) computed above). Applied on
    every load, so a correction survives re-running load.py against the
    same export without ever hand-editing data/raw/ or the database. Not
    committed — this is derived from someone's real spreadcheet, same as
    data/raw/ and data/imports/ (see .gitignore).

    Two kinds, both optional:
      delete:           [{id, reason}]          drop the row entirely
      amount_override:  [{id, amount, reason}]   replace amount, keep everything else
    """
    if not CORRECTIONS_PATH.exists():
        return {"delete": {}, "amount_override": {}}
    raw = yaml.safe_load(CORRECTIONS_PATH.read_text()) or {}
    return {
        "delete": {row["id"]: row.get("reason") for row in raw.get("delete", [])},
        "amount_override": {row["id"]: row for row in raw.get("amount_override", [])},
    }


def apply_corrections(transactions, corrections):
    """Returns (corrected_transactions, warnings). Warns rather than fails
    on a correction id that matches nothing — the id is a hash of
    (source_file, sheet, row), so it silently goes stale if that row's
    position shifts in a re-exported spreadsheet; that's a real thing to
    flag, but not the SPEC §6.1 kind of bug (a category no one mapped)."""
    warnings = []
    seen_ids = {t["id"] for t in transactions}

    for cid in corrections["delete"]:
        if cid not in seen_ids:
            warnings.append(f"correction: delete id {cid} matched no transaction (stale? spreadsheet row moved?)")
    for cid in corrections["amount_override"]:
        if cid not in seen_ids:
            warnings.append(f"correction: amount_override id {cid} matched no transaction (stale? spreadsheet row moved?)")

    result = []
    for t in transactions:
        if t["id"] in corrections["delete"]:
            continue
        override = corrections["amount_override"].get(t["id"])
        if override:
            t = dict(t, amount=abs(float(override["amount"])))
            if override.get("reason"):
                t["note"] = override["reason"]
        result.append(t)
    return result, warnings


def run_integrity_checks(transactions, today=None):
    """SPEC §6.2/§6.3 — warn, don't fail."""
    warnings = []
    today = today or date.today()

    # 6.2 duplicate detection: same date + amount + description
    seen = Counter((t["date"], t["amount"], t["description"]) for t in transactions)
    for (d, amount, desc), n in seen.items():
        if n > 1:
            warnings.append(f"duplicate x{n}: {d} {desc!r} ${amount}")

    # 6.3 month coverage gaps: a month with zero rent is probably missing data
    months_with_rent = set()
    all_months = set()
    for t in transactions:
        month = t["date"][:7]  # YYYY-MM
        all_months.add(month)
        if t["category"] == "rent":
            months_with_rent.add(month)
    # ...except the current month, which hasn't finished yet. Rent is paid at
    # the end of the month (every charge in the ledger so far lands on the
    # 25th-31st), so an in-progress month with no rent row is an incomplete
    # month, not missing data. Same principle as pacing off the calendar
    # rather than the newest row: only months that have ended can have a gap.
    current_month = today.strftime("%Y-%m")
    for month in sorted(all_months - months_with_rent):
        if month == current_month:
            continue
        warnings.append(f"no `rent` transaction in {month} — likely missing data")

    return warnings


def write_db(transactions, budgets_cfg, envelopes_cfg):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text())

        conn.execute("DELETE FROM transactions")
        conn.executemany(
            """INSERT OR REPLACE INTO transactions
               (id, date, description, amount, kind, category, note,
                source_file, source_sheet, source_row)
               VALUES (:id, :date, :description, :amount, :kind, :category,
                       :note, :source_file, :source_sheet, :source_row)""",
            transactions,
        )

        conn.execute("DELETE FROM budgets")
        scenario = budgets_cfg["scenario"]
        conn.executemany(
            "INSERT OR REPLACE INTO budgets VALUES (?, ?, ?, ?)",
            [
                (scenario, t["category"], t["monthly_target"], str(t["effective_from"]))
                for t in budgets_cfg["targets"]
            ],
        )

        conn.execute("DELETE FROM envelopes")
        for env in (envelopes_cfg.get("envelopes") or []):
            conn.execute(
                "INSERT OR REPLACE INTO envelopes VALUES (?, ?, ?, ?, ?)",
                (env["tag"], env["label"], env.get("budget"),
                 env.get("start_date"), env.get("end_date")),
            )

        conn.commit()
    finally:
        conn.close()


def main():
    print("Loading config...")
    categories_cfg, budgets_cfg, envelopes_cfg = load_config()

    print("Scanning data/raw/ for ledger workbooks...")
    try:
        transactions = build_transactions(categories_cfg)
        imported = load_approved_imports(categories_cfg)
    except LoaderError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    if imported:
        print(f"Loaded {len(imported)} approved import(s) from data/imports/approved.jsonl.")
    transactions += imported

    corrections = load_corrections()
    transactions, correction_warnings = apply_corrections(transactions, corrections)
    n_corrections = len(corrections["delete"]) + len(corrections["amount_override"])
    if n_corrections:
        print(f"Applied {n_corrections} correction(s) from data/corrections.yml.")

    print(f"Loaded {len(transactions)} transactions.")

    by_kind = Counter(t["kind"] for t in transactions)
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind}: {n}")

    warnings = run_integrity_checks(transactions) + correction_warnings
    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")

    # Written every run (even when empty) so the dashboard can show these
    # without anyone having to run this script by hand and read the
    # terminal — a real data problem was previously invisible in the UI.
    WARNINGS_PATH.write_text(json.dumps(dict(
        generated_at=datetime.now(timezone.utc).isoformat(),
        warnings=warnings,
    ), indent=2))

    write_db(transactions, budgets_cfg, envelopes_cfg)
    print(f"\nWrote {DB_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
