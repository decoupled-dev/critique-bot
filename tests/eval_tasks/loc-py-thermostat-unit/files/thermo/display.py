"""Formats the cabin temperature for the climate bar."""

from thermo.convert import c_to_f, round_half


def format_temperature(celsius, unit):
    if unit == "F":
        return f"{round_half(c_to_f(celsius)):g}°F"
    return f"{round_half(celsius):g}°C"


def format_fan(level):
    return "Fan " + "|" * level
