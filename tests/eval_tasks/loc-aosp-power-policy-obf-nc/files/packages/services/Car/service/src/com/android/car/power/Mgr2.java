package com.android.car.power;


final class Mgr2 {
    private boolean a6;
    private boolean a2;

    void set(boolean a9) {
        a2 = a6 != a9;
        a6 = a9;
    }

    boolean doIt6() {
        return a6;
    }

    boolean changed() {
        return a2;
    }
}
