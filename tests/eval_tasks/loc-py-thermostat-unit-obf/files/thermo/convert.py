"""Temperature conversion between Celsius and Fahrenheit."""


def doIt1(a2):
    return a2 * 9 / 5 + 32


def doIt2(a3):
    return (a3 - 32) * 5 / 9


def doIt8(value):
    """Round to the nearest half degree, as the cluster shows it."""
    return round(value * 2) / 2
