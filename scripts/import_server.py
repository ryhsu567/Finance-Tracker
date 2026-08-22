#!/usr/bin/env python3
"""Local server for the dashboard's Import tab (SPEC §10, scoped down).

    python scripts/import_server.py
    # then open http://localhost:8765/import.html

Everything else in this project is static (SPEC §9: "site reads only
JSON") and opened via file://. The Import tab is the one interactive
exception — approving/rejecting a staged transaction has to write to disk
and re-run the loader, which a static page can't do on its own. This is a
stdlib-only http.server, matching the rest of the project (no Node/npm,
per the decisions log in docs/SPEC.md §11) — it serves site/ as static
files and adds three JSON endpoints on top:

    GET  /api/pending           -> the review queue (data/imports/pending.jsonl)
    POST /api/approve  {id,...} -> move one row to data/imports/approved.jsonl,
                                    re-run load.py + build_dashboard.py
    POST /api/reject   {id}     -> drop one row from the review queue

Staging new candidates (from a screenshot Claude read in chat) happens
separately, via scripts/stage_import.py — this server only handles the
human review step.
"""
import json
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load import canonical_categories, VALID_KINDS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SITE_DIR = ROOT / "site"
PENDING_PATH = ROOT / "data" / "imports" / "pending.jsonl"
APPROVED_PATH = ROOT / "data" / "imports" / "approved.jsonl"
PORT = 8765

REQUIRED_FIELDS = {"date", "description", "amount", "kind", "category"}


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def rebuild_dashboard():
    """Re-run the loader + dashboard build after an approval so the site's
    static pages pick up the new transaction on next reload. Surfaces
    stdout/stderr on failure rather than swallowing it — an approval that
    silently fails to land in the DB is exactly the kind of invisible bug
    SPEC §6 exists to prevent."""
    for script in ("load.py", "build_dashboard.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script)],
            capture_output=True, text=True, cwd=ROOT,
        )
        if result.returncode != 0:
            return False, f"{script} failed:\n{result.stdout}\n{result.stderr}"
    return True, None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/pending":
            return self._json(200, read_jsonl(PENDING_PATH))
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON body"})

        if self.path == "/api/approve":
            return self._approve(body)
        if self.path == "/api/reject":
            return self._reject(body)
        self._json(404, {"error": "not found"})

    def _approve(self, body):
        pending = read_jsonl(PENDING_PATH)
        row = next((r for r in pending if r["id"] == body.get("id")), None)
        if row is None:
            return self._json(404, {"error": "no pending row with that id"})

        # Allow edits made in the review UI (category correction, etc.)
        # before approving — merge onto the staged row rather than trusting
        # the client for fields it didn't send.
        for field in ("date", "description", "amount", "kind", "category", "note"):
            if field in body:
                row[field] = body[field]

        missing = REQUIRED_FIELDS - row.keys()
        if missing:
            return self._json(400, {"error": f"missing field(s) {sorted(missing)}"})
        if row["kind"] not in VALID_KINDS:
            return self._json(400, {"error": f"invalid kind {row['kind']!r}"})
        categories_cfg = yaml.safe_load((CONFIG_DIR / "categories.yml").read_text())
        if row["category"] not in canonical_categories(categories_cfg):
            return self._json(400, {"error": f"unmapped category {row['category']!r}"})
        try:
            row["amount"] = abs(float(row["amount"]))
        except (TypeError, ValueError):
            return self._json(400, {"error": "amount must be numeric"})

        approved = read_jsonl(APPROVED_PATH)
        approved.append({k: row.get(k) for k in
                          ("id", "date", "description", "amount", "kind",
                           "category", "note", "source_note")} |
                         {"source_file": f"import:{row.get('source_note', 'unknown')}"})
        write_jsonl(APPROVED_PATH, approved)
        write_jsonl(PENDING_PATH, [r for r in pending if r["id"] != row["id"]])

        ok, error = rebuild_dashboard()
        if not ok:
            return self._json(500, {"error": error, "approved": True,
                                     "note": "row was approved but the rebuild failed — fix and re-run scripts/load.py"})
        return self._json(200, {"ok": True})

    def _reject(self, body):
        pending = read_jsonl(PENDING_PATH)
        remaining = [r for r in pending if r["id"] != body.get("id")]
        if len(remaining) == len(pending):
            return self._json(404, {"error": "no pending row with that id"})
        write_jsonl(PENDING_PATH, remaining)
        return self._json(200, {"ok": True})


def main():
    if not (SITE_DIR / "import.html").exists():
        print("site/import.html not found — run scripts/build_dashboard.py first.", file=sys.stderr)
        sys.exit(1)
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"Import tab server running at http://localhost:{PORT}/import.html")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
