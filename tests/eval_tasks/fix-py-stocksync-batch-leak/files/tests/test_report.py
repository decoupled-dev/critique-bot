import unittest

from stocksync.config import ConfigError, load_config
from stocksync.jobs import run_job


class ReportTest(unittest.TestCase):
    def test_all_sites_single(self):
        text = run_job("all-sites")
        self.assertIn("warehouses: BER, HEL, LON, MAD, OSL", text)
        self.assertIn("€", text)

    def test_uk_value_in_pounds(self):
        text = run_job("uk-value")
        self.assertIn("warehouses: LON", text)
        self.assertIn("£", text)

    def test_unknown_setting(self):
        with self.assertRaises(ConfigError):
            load_config("x", {"colour": "red"})


if __name__ == "__main__":
    unittest.main()
