package com.android.car.cluster;

import java.util.Locale;

/** Formats speed and distance for the instrument cluster. */
final class UnitsFormatter {
    private final Units mUnits;

    UnitsFormatter(Units units) {
        mUnits = units;
    }

    String speed(float metersPerSecond) {
        if (mUnits == Units.IMPERIAL) {
            return String.format(Locale.US, "%d mph", Math.round(metersPerSecond * 2.23694f));
        }
        return String.format(Locale.US, "%d km/h", Math.round(metersPerSecond * 3.6f));
    }

    String distance(float meters) {
        if (mUnits == Units.IMPERIAL) {
            return String.format(Locale.US, "%.1f mi", meters / 1609.344f);
        }
        return String.format(Locale.US, "%.1f km", meters / 1000f);
    }
}
