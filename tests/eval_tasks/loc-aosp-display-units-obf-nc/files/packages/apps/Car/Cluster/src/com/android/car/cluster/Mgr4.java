package com.android.car.cluster;

import java.util.Locale;


final class Mgr4 {
    private final Mgr3 a2;

    Mgr4(Mgr3 a5) {
        a2 = a5;
    }

    String speed(float a4) {
        if (a2 == Mgr3.IMPERIAL) {
            return String.format(Locale.US, "%d mph", Math.round(a4 * 2.23694f));
        }
        return String.format(Locale.US, "%d km/h", Math.round(a4 * 3.6f));
    }

    String distance(float a3) {
        if (a2 == Mgr3.IMPERIAL) {
            return String.format(Locale.US, "%.1f mi", a3 / 1609.344f);
        }
        return String.format(Locale.US, "%.1f km", a3 / 1000f);
    }
}
