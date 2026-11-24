"""The loader's fail-loud contract (CLAUDE.md, SPEC §6.1).

A silently dropped transaction is the bug this project exists to fix, so the
behavior worth pinning down is that bad input *raises* — not that good input
loads. Each test here is a way a row has actually gone missing or landed in
the wrong bucket before.

Run: python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

from load import (  # noqa: E402
    LoaderError,
    casefold_lookup,
    categories_by_kind,
    check_category_for_kind,
    map_expense_category,
)

CATEGORIES = yaml.safe_load((ROOT / "config" / "categories.yml").read_text())
WHERE = dict(sheet="Expenses", row=12, source_file="test.xlsx")


class UnmappedCategoryRaises(unittest.TestCase):
    def test_unknown_raw_label_raises(self):
        with self.assertRaises(LoaderError) as cm:
            map_expense_category("Vacation", "Some charge", CATEGORIES, **WHERE)
        self.assertIn("Unmapped Expenses category", str(cm.exception))

    def test_message_names_the_row_so_it_can_be_found(self):
        with self.assertRaises(LoaderError) as cm:
            map_expense_category("Vacation", "Some charge", CATEGORIES, **WHERE)
        msg = str(cm.exception)
        self.assertIn("test.xlsx", msg)
        self.assertIn("row 12", msg)

    def test_every_raw_label_maps_to_a_real_category(self):
        """A raw label pointing at a category that was later renamed would
        pass the mapping step and then fail (or worse, not) downstream."""
        canonical = set(CATEGORIES["expense"]) | {
            c for group in CATEGORIES["expense"].values()
            if isinstance(group, dict) for c in group
        }
        for raw, cat in CATEGORIES["raw_expense_labels"].items():
            self.assertIn(cat, canonical, f"raw label {raw!r} -> unknown {cat!r}")


class DescriptionOverridesWin(unittest.TestCase):
    def test_description_beats_the_category_column(self):
        # "Badminton" is entered under Gym in the source sheet.
        cat, kind = map_expense_category("Gym", "Badminton", CATEGORIES, **WHERE)
        self.assertEqual((cat, kind), ("entertainment", "expense"))

    def test_lookup_ignores_case(self):
        # Hand-typed, so HEYTEA / HeyTea / Heytea all arrive.
        for spelling in ("HEYTEA", "HeyTea", "heytea"):
            cat, _ = map_expense_category("Other", spelling, CATEGORIES, **WHERE)
            self.assertEqual(cat, "sweet_treat", spelling)

    def test_keys_differing_only_in_case_are_ambiguous_not_last_wins(self):
        with self.assertRaises(LoaderError) as cm:
            casefold_lookup({"Costco": "groceries", "COSTCO": "shopping"},
                            "costco", what="test_map")
        self.assertIn("differ only in case", str(cm.exception))

    def test_agreeing_case_variants_are_fine(self):
        self.assertEqual(
            casefold_lookup({"Costco": "groceries", "COSTCO": "groceries"},
                            "costco", what="test_map"),
            "groceries")


class CategoryMustMatchKind(unittest.TestCase):
    """The bug that reached the database on 2026-11-11: an income row filed
    under an expense category passed a flat union check."""

    def test_income_under_an_expense_category_raises(self):
        with self.assertRaises(LoaderError) as cm:
            check_category_for_kind("income", "groceries", CATEGORIES, where="row 1")
        self.assertIn("not an income one", str(cm.exception))

    def test_expense_under_an_income_category_raises(self):
        with self.assertRaises(LoaderError) as cm:
            check_category_for_kind("expense", "salary", CATEGORIES, where="row 1")
        self.assertIn("not an expense one", str(cm.exception))

    def test_unknown_category_raises(self):
        with self.assertRaises(LoaderError) as cm:
            check_category_for_kind("expense", "bitcoin_pizza", CATEGORIES, where="row 1")
        self.assertIn("unmapped category", str(cm.exception))

    def test_invalid_kind_raises(self):
        with self.assertRaises(LoaderError):
            check_category_for_kind("outgoing", "groceries", CATEGORIES, where="row 1")

    def test_valid_pairs_pass(self):
        for kind, category in (("expense", "groceries"), ("income", "salary"),
                               ("transfer", "stock_sale"), ("investment", "brokerage")):
            check_category_for_kind(kind, category, CATEGORIES, where="row 1")

    def test_kinds_do_not_share_categories(self):
        """Overlap would make the per-kind check ambiguous again."""
        by_kind = categories_by_kind(CATEGORIES)
        for kind, cats in by_kind.items():
            for other, other_cats in by_kind.items():
                if other <= kind:
                    continue
                self.assertEqual(cats & other_cats, set(),
                                 f"{kind} and {other} share categories")


if __name__ == "__main__":
    unittest.main()
