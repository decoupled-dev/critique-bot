import unittest

from thermo.convert import c_to_f, f_to_c, round_half


class ConvertTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertAlmostEqual(f_to_c(c_to_f(21.5)), 21.5)

    def test_half_degrees(self):
        self.assertEqual(round_half(21.3), 21.5)
