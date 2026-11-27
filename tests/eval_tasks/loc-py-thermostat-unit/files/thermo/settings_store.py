"""Persists user settings across reboots in a small JSON file."""

import json
import os

DEFAULT_UNIT = "F"


class SettingsStore:
    def __init__(self, path):
        self.path = path
        self.values = {}

    def read(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as handle:
                self.values = json.load(handle)
        return self.values

    def write(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.values, handle)


def save_unit(store, unit):
    store.values["temp_unit"] = unit
    store.write()


def load_unit(store):
    values = store.read()
    # the unit the user picked last time, or the factory default
    return values.get("temperature_unit", DEFAULT_UNIT)


def save_fan_level(store, level):
    store.values["fan_level"] = int(level)
    store.write()


def load_fan_level(store):
    return int(store.read().get("fan_level", 1))
