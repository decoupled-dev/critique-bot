package com.android.car.power;

import android.os.SystemProperties;
import android.util.Slog;

/** Decides which power policy is active and hands it to the component handler. */
public final class CarPowerManagementService {
    private static final String TAG = "CarPowerManagementService";
    private static final String PROP_DEFAULT_POLICY = "persist.vendor.car.power.default_policy";

    private final PolicyReader mReader = new PolicyReader();
    private final PowerComponentHandler mHandler = new PowerComponentHandler();

    public void onBootCompleted() {
        String id = SystemProperties.get(PROP_DEFAULT_POLICY, "all_on");
        applyPowerPolicy(id);
    }

    public boolean applyPowerPolicy(String id) {
        PowerPolicy policy = mReader.get(id);
        if (policy == null) {
            Slog.w(TAG, "unknown power policy " + id);
            return false;
        }
        mHandler.applyPolicy(policy);
        return true;
    }
}
