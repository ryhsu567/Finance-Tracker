"""The sinking-fund balance is computed twice — once in Python for the static
charts (build_dashboard.compute's fund_for) and once in JavaScript for the
live meters (app.js's categoryResult). Both are documented as mirrors of each
other; nothing but this test makes them stay that way.

They are not merged because they serve different moments: Python renders the
history at build time, JS recomputes against a target the user can edit in the
browser. So the duplication stays, and the agreement gets asserted.

Needs data/budget.db (run scripts/load.py) and node; skips without either.

Run: python3 -m unittest discover tests
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_dashboard as bd  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "js_fund_balances.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node not installed")
@unittest.skipUnless((ROOT / "data" / "budget.db").exists(),
                     "data/budget.db missing — run scripts/load.py")
@unittest.skipUnless((ROOT / "site" / "data.js").exists(),
                     "site/data.js missing — run scripts/build_dashboard.py")
class PythonAndJavascriptAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        categories_cfg, targets, txns, accounts_cfg = bd.load_data()
        cls.d = bd.compute(categories_cfg, targets, txns, accounts_cfg)
        cls.js = json.loads(subprocess.run(
            [NODE, str(HARNESS)], capture_output=True, text=True, check=True).stdout)

    def test_there_is_something_to_compare(self):
        self.assertTrue(self.d["lumpy_cats"], "no lumpy categories in the config")
        self.assertEqual(set(self.js), set(self.d["lumpy_cats"]),
                         "Python and JS disagree about which categories are lumpy")

    def test_balances_match_month_by_month(self):
        for cat in self.d["lumpy_cats"]:
            py = [row["balance"] for row in self.d["lumpy_funds"][cat]]
            js = self.js[cat]
            self.assertEqual(len(py), len(js), f"{cat}: month count differs")
            for i, (p, j) in enumerate(zip(py, js)):
                self.assertAlmostEqual(
                    p, j, places=6,
                    msg=f"{cat} month {self.d['months'][i]}: python {p} != js {j}")

    def test_accrual_is_not_floored_at_zero(self):
        """Travel is deliberately allowed to go negative — flooring it would
        hide exactly the overspend the fund exists to show."""
        balances = [b for cat in self.js for b in self.js[cat]]
        self.assertTrue(any(b < 0 for b in balances),
                        "expected at least one overdrawn month in the 2026 ledger")

    def test_fund_does_not_reset_at_a_year_boundary(self):
        """A fund has no calendar year: accrual counts months since the first
        row in the ledger, so `accrued` must rise monotonically."""
        for cat in self.d["lumpy_cats"]:
            accrued = [row["accrued"] for row in self.d["lumpy_funds"][cat]]
            self.assertEqual(accrued, sorted(accrued), f"{cat}: accrual not monotonic")

    def test_balance_is_accrued_minus_spent(self):
        """No stored subtotals (CLAUDE.md) — the balance must be derivable."""
        for cat in self.d["lumpy_cats"]:
            for row in self.d["lumpy_funds"][cat]:
                self.assertAlmostEqual(row["balance"], row["accrued"] - row["spent"],
                                       places=6, msg=f"{cat} {row['month']}")


if __name__ == "__main__":
    unittest.main()
