"""Simple trip statistics."""


def average_speed(rows):
    speeds = [float(r[1]) for r in rows]
    return sum(speeds) / len(speeds) if speeds else 0.0
