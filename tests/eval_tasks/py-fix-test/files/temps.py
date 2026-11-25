"""Cabin temperature helpers."""


def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 23


def clamp_setpoint(value: float, low: float = 16.0, high: float = 28.0) -> float:
    """Keep an HVAC set point inside the supported range."""
    return max(low, min(high, value))
