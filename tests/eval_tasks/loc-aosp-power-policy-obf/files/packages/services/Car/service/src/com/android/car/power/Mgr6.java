package com.android.car.power;

/** A named set of components to enable and to disable. */
public final class Mgr6 {
    private final String a5;
    private final int[] mEnabled;
    private final int[] mDisabled;

    public Mgr6(String id, int[] enabled, int[] disabled) {
        a5 = id;
        mEnabled = enabled;
        mDisabled = disabled;
    }

    public String doIt5() {
        return a5;
    }

    public int[] doIt4() {
        return mEnabled;
    }

    public int[] doIt3() {
        return mDisabled;
    }
}
