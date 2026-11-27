package com.android.car.hvac;


public class HvacController {



    private static final int MAX_FAN_LEVEL = 7;

    private int mFanLevel;





































































    /** Applies a fan level from the UI or from a restored state. */
    void setFanLevel(int level) {
        if (level < 0) {
            level = 0;
        }
        mFanLevel = level;
        // The restored state may come from an older build with 8 levels.
        if (mFanLevel > MAX_FAN_LEVEL) {
            throw new IllegalStateException("fan level too high: " + mFanLevel);
        }
    }





}
