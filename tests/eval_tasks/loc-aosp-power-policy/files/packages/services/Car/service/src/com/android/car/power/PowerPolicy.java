package com.android.car.power;

/** A named set of components to enable and to disable. */
public final class PowerPolicy {
    private final String mId;
    private final int[] mEnabled;
    private final int[] mDisabled;

    public PowerPolicy(String id, int[] enabled, int[] disabled) {
        mId = id;
        mEnabled = enabled;
        mDisabled = disabled;
    }

    public String getId() {
        return mId;
    }

    public int[] getEnabled() {
        return mEnabled;
    }

    public int[] getDisabled() {
        return mDisabled;
    }
}
