"""Formats the cabin temperature for the climate bar."""

from thermo.convert import doIt1, doIt8


def doIt4(a2, a7):
    if a7 == "F":
        return f"{doIt8(doIt1(a2)):g}°F"
    return f"{doIt8(a2):g}°C"


def doIt3(a5):
    return "Fan " + "|" * a5
