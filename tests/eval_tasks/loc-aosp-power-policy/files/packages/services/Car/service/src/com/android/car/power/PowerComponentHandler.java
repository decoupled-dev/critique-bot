package com.android.car.power;

import android.util.Slog;
import android.util.SparseArray;

/** Keeps the state of every power component and applies policies to them. */
public final class PowerComponentHandler {
    private static final String TAG = "CarPowerComponent";

    private final SparseArray<ComponentState> mComponentStates = new SparseArray<>();

    public PowerComponentHandler() {
        // components present on every head unit; others register when their HAL comes up
        register(PowerComponent.AUDIO);
        register(PowerComponent.MEDIA);
        register(PowerComponent.DISPLAY);
        register(PowerComponent.BLUETOOTH);
    }

    public void register(int component) {
        if (mComponentStates.get(component) == null) {
            mComponentStates.put(component, new ComponentState());
        }
    }

    public boolean isOn(int component) {
        ComponentState state = mComponentStates.get(component);
        return state != null && state.isOn();
    }

    public void applyPolicy(PowerPolicy policy) {
        Slog.i(TAG, "applying power policy " + policy.getId());
        for (int component : policy.getEnabled()) {
            setState(component, true);
        }
        for (int component : policy.getDisabled()) {
            setState(component, false);
        }
    }

    private void setState(int component, boolean on) {
        mComponentStates.get(component).set(on);
        if (mComponentStates.get(component).changed()) {
            Slog.d(TAG, PowerComponent.name(component) + (on ? " on" : " off"));
        }
    }
}
