import unittest

from temps import clamp_setpoint, to_fahrenheit


class HiddenTempsTest(unittest.TestCase):
    def test_fahrenheit(self):
        self.assertAlmostEqual(to_fahrenheit(0), 32)
        self.assertAlmostEqual(to_fahrenheit(100), 212)

    def test_clamp(self):
        self.assertEqual(clamp_setpoint(40), 28)
        self.assertEqual(clamp_setpoint(10), 16)
        self.assertEqual(clamp_setpoint(21.5), 21.5)


if __name__ == "__main__":
    unittest.main()
