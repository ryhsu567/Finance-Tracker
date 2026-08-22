#!/usr/bin/env python3
"""Stage candidate transactions extracted from a screenshot/file into the
Import tab's review queue (SPEC §10, scoped down — see CLAUDE.md).

Claude (in a chat session) reads the uploaded file, extracts transactions,
and pipes them into this script as JSON on stdin:

    echo '[{"date": "2026-08-14", "description": "Trader Joe'"'"'s", "amount": 42.10,
            "kind": "expense", "category": "groceries", "note": null,
            "source_note": "screenshot: chase_statement.png"}]' \\
        | python scripts/stage_import.py

Each row is validated against config/categories.yml (fail loud — SPEC §6.1
applies here too, not just to the spreadsheet loader) and appended to
data/imports/pending.jsonl for review in the dashboard's Import tab. Nothing
here touches data/budget.db — rows only reach the database after a human
approves them (Import tab -> data/imports/approved.jsonl -> next load.py run).
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load import canonical_categories, VALID_KINDS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PENDING_PATH = ROOT / "data" / "imports" / "pending.jsonl"

REQUIRED_FIELDS = {"date", "description", "amount", "kind", "category"}


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("No input on stdin — pipe a JSON array of transaction candidates.")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON on stdin: {e}")
    if not isinstance(rows, list):
        raise SystemExit("Input must be a JSON array of transaction objects.")

    categories_cfg = yaml.safe_load((CONFIG_DIR / "categories.yml").read_text())
    valid_categories = canonical_categories(categories_cfg)

    staged = []
    for i, row in enumerate(rows):
        missing = REQUIRED_FIELDS - row.keys()
        if missing:
            raise SystemExit(f"Row {i}: missing required field(s) {sorted(missing)}.")
        if row["kind"] not in VALID_KINDS:
            raise SystemExit(f"Row {i}: invalid kind {row['kind']!r} — must be one of {sorted(VALID_KINDS)}.")
        if row["category"] not in valid_categories:
            raise SystemExit(
                f"Row {i}: unmapped category {row['category']!r} "
                f"({row['description']!r}). Add it to config/categories.yml first "
                f"or fix the extraction — do not guess."
            )
        staged.append(dict(
            id=uuid.uuid4().hex[:16],
            status="pending",
            staged_at=datetime.now(timezone.utc).isoformat(),
            date=row["date"],
            description=row["description"],
            amount=abs(float(row["amount"])),
            kind=row["kind"],
            category=row["category"],
            note=row.get("note"),
            source_note=row.get("source_note", "import"),
        ))

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PENDING_PATH.open("a") as f:
        for row in staged:
            f.write(json.dumps(row) + "\n")

    print(f"Staged {len(staged)} transaction(s) to {PENDING_PATH.relative_to(ROOT)}.")
    print("Review and approve them in the dashboard's Import tab.")


if __name__ == "__main__":
    main()
