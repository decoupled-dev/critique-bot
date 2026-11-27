package com.android.car.cluster;

import android.os.SystemProperties;
import android.util.Log;


final class Mgr2 {
    private static final String TAG = "ClusterUnits";
    private static final String KEY_UNITS = "persist.vendor.car.display_unit";

    Mgr3 readUnits() {
        String value = SystemProperties.get(KEY_UNITS, "metric");
        Log.i(TAG, "display units from property: " + value);
        return Mgr3.parse(value);
    }

    boolean use24Hour() {
        return SystemProperties.getBoolean("persist.vendor.car.clock_24h", true);
    }
}
