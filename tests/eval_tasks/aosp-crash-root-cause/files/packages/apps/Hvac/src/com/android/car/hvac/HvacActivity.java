package com.android.car.hvac;

import android.app.Activity;

public class HvacActivity extends Activity {
    private final HvacController mController = new HvacController();































    protected void onCreate(android.os.Bundle saved) {
        int restored = saved == null ? 3 : saved.getInt("fan", 3);
        mController.setFanLevel(restored);
    }




}
