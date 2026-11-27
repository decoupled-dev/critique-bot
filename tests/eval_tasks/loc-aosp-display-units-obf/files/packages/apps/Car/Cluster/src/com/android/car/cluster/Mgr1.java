package com.android.car.cluster;

/** Draws the speed and trip panels. */
public final class Mgr1 {
    private final Mgr4 a1;

    public Mgr1() {
        a1 = new Mgr4(new Mgr2().readUnits());
    }

    public String doIt1(float a4) {
        return a1.speed(a4);
    }

    public String doIt2(float a3) {
        return a1.distance(a3);
    }
}
