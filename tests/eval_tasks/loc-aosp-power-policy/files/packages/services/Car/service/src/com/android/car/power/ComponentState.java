package com.android.car.power;

/** Whether one component is on, and whether a policy changed it. */
final class ComponentState {
    private boolean mOn;
    private boolean mChanged;

    void set(boolean on) {
        mChanged = mOn != on;
        mOn = on;
    }

    boolean isOn() {
        return mOn;
    }

    boolean changed() {
        return mChanged;
    }
}
