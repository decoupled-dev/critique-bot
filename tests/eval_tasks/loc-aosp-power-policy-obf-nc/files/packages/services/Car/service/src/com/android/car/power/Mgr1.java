package com.android.car.power;

import android.os.SystemProperties;
import android.util.Slog;


public final class Mgr1 {
    private static final String TAG = "CarPowerManagementService";
    private static final String PROP_DEFAULT_POLICY = "persist.vendor.car.power.default_policy";

    private final Mgr3 a8 = new Mgr3();
    private final Mgr5 a4 = new Mgr5();

    public void doIt7() {
        String id = SystemProperties.get(PROP_DEFAULT_POLICY, "all_on");
        doIt2(id);
    }

    public boolean doIt2(String id) {
        Mgr6 a10 = a8.get(id);
        if (a10 == null) {
            Slog.w(TAG, "unknown power policy " + id);
            return false;
        }
        a4.doIt1(a10);
        return true;
    }
}
