"""Small helpers used across stocksync."""

from collections.abc import Mapping


def deep_merge(base: dict, extra: Mapping) -> dict:
    """Return a new dict: ``base`` with ``extra`` applied on top.

    Nested sections (dicts) are merged key by key, so an override only needs to
    name the keys it changes.
    """
    result = dict(base)
    for key, value in extra.items():
        current = result.get(key)
        if isinstance(value, Mapping) and isinstance(current, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def money(amount: float, currency: str) -> str:
    symbols = {"EUR": "€", "USD": "$", "GBP": "£"}
    sym = symbols.get(currency, currency + " ")
    return f"{sym}{amount:,.2f}"
