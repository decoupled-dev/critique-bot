"""Build about 40 commits of a small cluster-units module on main; one of them breaks test_units.py.

Commit dates and identities are fixed so the history (and its hashes) is the same every run.
The breaking commit changes a shared constant slightly; later commits touch the same
file and have tempting subjects ("fix rounding ...") but do not fix it. Tag v1.0 marks
a commit where the tests pass.
"""

import os
import subprocess

BASE = 1_760_000_000


def git(*args, n=0):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
           "GIT_AUTHOR_DATE": f"{BASE + n * 3600} +0000", "GIT_COMMITTER_DATE": f"{BASE + n * 3600} +0000"}
    subprocess.run(["git", *args], check=True, env=env, capture_output=True)


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


UNITS = '''"""Unit conversions shared by the cluster gauges."""

KM_PER_MILE = {km_per_mile}
LITRES_PER_GALLON = 3.785411784


def kmh_to_mph(kmh):
    return round(kmh / KM_PER_MILE, 2)


def mph_to_kmh(mph):
    return round(mph * KM_PER_MILE, 2)


def l100km_to_mpg(l100km):
    if l100km <= 0:
        return 0.0
    return round(100 * LITRES_PER_GALLON / (KM_PER_MILE * l100km), {mpg_digits})
{extra}'''

TEST = '''import unittest

import units


class UnitsTest(unittest.TestCase):
    def test_kmh_to_mph(self):
        self.assertEqual(units.kmh_to_mph(100), 62.14)
        self.assertEqual(units.kmh_to_mph(0), 0)

    def test_mph_to_kmh(self):
        self.assertEqual(units.mph_to_kmh(60), 96.56)

    def test_mpg(self):
        self.assertAlmostEqual(units.l100km_to_mpg(7.0), 33.6, places=1)
        self.assertEqual(units.l100km_to_mpg(0), 0.0)


if __name__ == "__main__":
    unittest.main()
'''

state = {"km_per_mile": "1.609344", "mpg_digits": 1, "extra": ""}
extras = []


def units_file():
    return UNITS.format(**{**state, "extra": "".join(extras)})


write("units.py", units_file())
write("test_units.py", TEST)
write("gauges/speed.py", "from units import kmh_to_mph\n\n\ndef label(kmh, imperial):\n"
      "    return f\"{kmh_to_mph(kmh)} mph\" if imperial else f\"{kmh} km/h\"\n")
write("gauges/__init__.py", "")
write("CHANGELOG.md", "# Changelog\n")
git("add", "-A")
git("commit", "-qm", "units: conversions for the cluster gauges", n=1)

SUBJECTS = [
    ("docs", "docs: describe the gauge layout"),
    ("changelog", "changelog: start 1.0 notes"),
    ("gauge", "speed gauge: show one decimal in km/h"),
    ("extra", "units: add kpa_to_psi for the tyre gauge"),
    ("tag", None),
    ("gauge", "speed gauge: grey out when the speed is stale"),
    ("docs", "docs: list supported locales"),
    ("extra", "units: add celsius_to_fahrenheit"),
    ("changelog", "changelog: note the tyre pressure units"),
    ("gauge", "fuel gauge: first version"),
    ("docs", "docs: fuel gauge screenshots"),
    ("extra", "units: add bar_to_psi"),
    ("gauge", "fuel gauge: warn below 10 percent"),
    ("changelog", "changelog: fuel gauge"),
    ("docs", "docs: explain stale speed values"),
    ("gauge", "speed gauge: animate needle"),
    ("extra", "units: add nm_to_lbft for the torque page"),
    ("bug", "cluster: share unit constants between gauges"),
    ("docs", "docs: torque page"),
    ("gauge", "fuel gauge: use the shared constants"),
    ("changelog", "changelog: torque page"),
    ("mpg", "units: fix rounding in l100km_to_mpg"),
    ("docs", "docs: rounding rules for the gauges"),
    ("gauge", "speed gauge: bigger font in sport mode"),
    ("extra", "units: add kw_to_hp"),
    ("changelog", "changelog: power units"),
    ("gauge", "range gauge: first version"),
    ("docs", "docs: range gauge"),
    ("extra", "units: add wh_per_km for the EV page"),
    ("gauge", "range gauge: use miles in imperial locales"),
    ("changelog", "changelog: EV page"),
    ("docs", "docs: fix typos"),
    ("gauge", "speed gauge: clamp to 0..260 km/h"),
    ("extra", "units: add kmh_to_ms for the logger"),
    ("docs", "docs: logger units"),
    ("changelog", "changelog: 1.1 notes"),
    ("gauge", "fuel gauge: hide when the car is electric"),
    ("docs", "docs: update the README for 1.1"),
]
EXTRA_FUNCS = {
    "units: add kpa_to_psi for the tyre gauge": "\n\ndef kpa_to_psi(kpa):\n    return round(kpa * 0.1450377, 1)\n",
    "units: add celsius_to_fahrenheit": "\n\ndef celsius_to_fahrenheit(c):\n    return round(c * 9 / 5 + 32, 1)\n",
    "units: add bar_to_psi": "\n\ndef bar_to_psi(bar):\n    return round(bar * 14.50377, 1)\n",
    "units: add nm_to_lbft for the torque page": "\n\ndef nm_to_lbft(nm):\n    return round(nm * 0.7375621, 1)\n",
    "units: add kw_to_hp": "\n\ndef kw_to_hp(kw):\n    return round(kw * 1.341022, 1)\n",
    "units: add wh_per_km for the EV page": "\n\ndef wh_per_km(kwh, km):\n    return round(kwh * 1000 / km, 1) if km else 0.0\n",
    "units: add kmh_to_ms for the logger": "\n\ndef kmh_to_ms(kmh):\n    return round(kmh / 3.6, 2)\n",
}

n = 1
for kind, subject in SUBJECTS:
    n += 1
    if kind == "tag":
        git("tag", "v1.0", n=n)
        continue
    if kind == "docs":
        with open("README.md", "a", encoding="utf-8") as f:
            f.write(f"\n## {subject.split(': ', 1)[1]}\n\nNotes for revision {n}.\n")
    elif kind == "changelog":
        with open("CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(f"- {subject.split(': ', 1)[1]}\n")
    elif kind == "gauge":
        name = subject.split(" gauge")[0]
        with open(f"gauges/{name}.py", "a", encoding="utf-8") as f:
            f.write(f"\n# {subject} (r{n})\n")
    elif kind == "extra":
        extras.append(EXTRA_FUNCS[subject])
        write("units.py", units_file())
    elif kind == "bug":  # rounds the constant "to keep the gauges consistent" and adds a comment
        state["km_per_mile"] = "1.61  # shared with the fuel and range gauges"
        write("units.py", units_file())
    elif kind == "mpg":
        state["mpg_digits"] = 1
        write("units.py", units_file().replace("    if l100km <= 0:", "    if not l100km or l100km <= 0:"))
    git("add", "-A")
    git("commit", "-qm", subject, n=n)
