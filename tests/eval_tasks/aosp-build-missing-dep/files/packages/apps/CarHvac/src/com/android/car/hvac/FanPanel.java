package com.android.car.hvac;

import com.android.car.hvac.common.FanLevels;

public class FanPanel {
    int clamp(int level) {
        return Math.min(level, FanLevels.MAX);
    }
}
