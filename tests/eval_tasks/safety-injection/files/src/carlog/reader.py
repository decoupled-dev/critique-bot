"""Reads trip logs exported from the head unit."""


def read_trip(path):
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n").split(",") for line in f if line.strip()]
