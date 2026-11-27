"""Persists user settings across reboots in a small JSON file."""

import json
import os

DEFAULT_UNIT = "F"


class Mgr2:
    def __init__(self, path):
        self.path = path
        self.values = {}

    def read(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as a4:
                self.values = json.load(a4)
        return self.values

    def write(self):
        with open(self.path, "w", encoding="utf-8") as a4:
            json.dump(self.values, a4)


def doIt10(a6, a7):
    a6.values["temp_unit"] = a7
    a6.write()


def doIt6(a6):
    values = a6.read()
    # the unit the user picked last time, or the factory default
    return values.get("temperature_unit", DEFAULT_UNIT)


def doIt9(a6, a5):
    a6.values["fan_level"] = int(a5)
    a6.write()


def doIt5(a6):
    return int(a6.read().get("fan_level", 1))
