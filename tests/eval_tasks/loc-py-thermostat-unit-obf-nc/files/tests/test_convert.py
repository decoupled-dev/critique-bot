import unittest

from thermo.convert import doIt1, doIt2, doIt8


class Mgr1(unittest.TestCase):
    def test_round_trip(self):
        self.assertAlmostEqual(doIt2(doIt1(21.5)), 21.5)

    def test_half_degrees(self):
        self.assertEqual(doIt8(21.3), 21.5)
