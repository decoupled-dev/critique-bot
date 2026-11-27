package com.android.car.cluster;

/** Draws the speed and trip panels. */
public final class ClusterRenderer {
    private final UnitsFormatter mFormatter;

    public ClusterRenderer() {
        mFormatter = new UnitsFormatter(new DisplayUnitsReader().readUnits());
    }

    public String speedText(float metersPerSecond) {
        return mFormatter.speed(metersPerSecond);
    }

    public String tripText(float meters) {
        return mFormatter.distance(meters);
    }
}
