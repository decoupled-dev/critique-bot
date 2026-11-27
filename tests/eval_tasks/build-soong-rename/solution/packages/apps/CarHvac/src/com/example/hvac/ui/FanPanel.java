package com.example.hvac.ui;

import com.example.hvac.common.FanController;

/** The fan row of the HVAC panel: plus and minus buttons around the current level. */
public class FanPanel {
    private final FanController mFan;
    private int mShown;

    public FanPanel(FanController fan) {
        mFan = fan;
    }

    public void onPlusClicked() {
        mShown = mFan.setFanLevel(mFan.getFanLevel() + 1);
    }

    public void onMinusClicked() {
        mShown = mFan.setFanLevel(mFan.getFanLevel() - 1);
    }

    public int shownLevel() {
        return mShown;
    }
}
