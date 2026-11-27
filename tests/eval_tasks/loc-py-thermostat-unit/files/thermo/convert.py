"""Temperature conversion between Celsius and Fahrenheit."""


def c_to_f(celsius):
    return celsius * 9 / 5 + 32


def f_to_c(fahrenheit):
    return (fahrenheit - 32) * 5 / 9


def round_half(value):
    """Round to the nearest half degree, as the cluster shows it."""
    return round(value * 2) / 2
