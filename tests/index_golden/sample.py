"""Module doc."""
import logging
from .units import to_display as conv

log = logging.getLogger(__name__)
TAG = "HvacPy"


class Controller(Base):
    def set_speed(self, level):
        if level < 0:
            raise ValueError("fan level must be positive")
        log.warning("fan speed %s", level)
        return conv(level, "persist.car.fan_unit")

    def _helper(self):
        return self.set_speed(1)


def main():
    Controller().set_speed(3)
