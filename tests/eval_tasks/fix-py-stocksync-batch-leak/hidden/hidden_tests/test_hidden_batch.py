import copy
import unittest

from stocksync import defaults
from stocksync.config import load_config
from stocksync.jobs import NIGHTLY_JOBS, run_batch, run_job


class HiddenBatchTest(unittest.TestCase):
    def setUp(self):
        self._saved = copy.deepcopy(defaults.DEFAULTS)

    def tearDown(self):
        defaults.DEFAULTS.clear()
        defaults.DEFAULTS.update(self._saved)

    def test_batch_matches_single_runs(self):
        batch = run_batch()
        self.assertEqual(list(batch), NIGHTLY_JOBS)
        for name in NIGHTLY_JOBS:
            self.assertEqual(batch[name], run_job(name), name)

    def test_all_sites_after_filtered_jobs(self):
        batch = run_batch(["north-low-stock", "uk-value", "all-sites"])
        text = batch["all-sites"]
        self.assertIn("warehouses: BER, HEL, LON, MAD, OSL", text)
        self.assertIn("€", text)
        self.assertNotIn("£", text)
        self.assertIn("A-210    HEL", text)  # qty 0 is kept when min_qty is the default 0

    def test_defaults_not_modified(self):
        load_config("a", {"filters": {"warehouses": ["OSL"], "min_qty": 3},
                          "output": {"currency": "USD"}})
        self.assertEqual(defaults.DEFAULTS, self._saved)
        cfg = load_config("b", {})
        self.assertEqual(cfg.warehouses, ())
        self.assertEqual(cfg.min_qty, 0)
        self.assertEqual(cfg.currency, "EUR")
        self.assertEqual(cfg.sort_by, "sku")

    def test_override_still_partial(self):
        cfg = load_config("c", {"output": {"currency": "GBP"}})
        self.assertEqual(cfg.currency, "GBP")
        self.assertEqual(cfg.title, "Stock report")
        self.assertEqual(cfg.sort_by, "sku")
        cfg = load_config("d", {"filters": {"min_qty": 5}})
        self.assertEqual(cfg.min_qty, 5)
        self.assertFalse(cfg.include_discontinued)

    def test_clearance_after_uk(self):
        batch = run_batch(["uk-value", "clearance"])
        self.assertIn("C-500", batch["clearance"])
        self.assertIn("$", batch["clearance"])
        self.assertIn("warehouses: BER, HEL, LON, MAD, OSL", batch["clearance"])


if __name__ == "__main__":
    unittest.main()
