"""Job configuration: defaults plus per-job overrides."""

from dataclasses import dataclass

from .defaults import DEFAULTS
from .util import deep_merge


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class JobConfig:
    name: str
    batch_size: int
    warehouses: tuple
    min_qty: int
    include_discontinued: bool
    currency: str
    sort_by: str
    title: str


_KNOWN = {"batch_size", "filters", "output"}


def load_config(name: str, overrides: dict | None = None) -> JobConfig:
    overrides = overrides or {}
    unknown = set(overrides) - _KNOWN
    if unknown:
        raise ConfigError(f"{name}: unknown settings {sorted(unknown)}")
    merged = deep_merge(DEFAULTS, overrides)
    filters = merged["filters"]
    output = merged["output"]
    if output["sort_by"] not in ("sku", "qty", "value"):
        raise ConfigError(f"{name}: bad sort_by {output['sort_by']!r}")
    return JobConfig(
        name=name,
        batch_size=int(merged["batch_size"]),
        warehouses=tuple(filters["warehouses"]),
        min_qty=int(filters["min_qty"]),
        include_discontinued=bool(filters["include_discontinued"]),
        currency=output["currency"],
        sort_by=output["sort_by"],
        title=output["title"],
    )
