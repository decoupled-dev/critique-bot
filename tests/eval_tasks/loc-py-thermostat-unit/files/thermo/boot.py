"""Restores the climate bar after the head unit starts."""

import logging

from thermo.display import format_temperature
from thermo.settings_store import SettingsStore, load_fan_level, load_unit

log = logging.getLogger("thermo")


def on_boot(path, cabin_celsius):
    store = SettingsStore(path)
    unit = load_unit(store)
    log.info("restored display unit %s", unit)
    level = load_fan_level(store)
    return format_temperature(cabin_celsius, unit), level
