package com.example.hvac.voice;

import com.example.hvac.common.FanController;

/** Handles "set the fan to N" voice commands. */
public class VoiceCommandHandler {
    private final FanController mFan;

    public VoiceCommandHandler(FanController fan) {
        mFan = fan;
    }

    /** Returns the reply spoken back to the driver. */
    public String onSetFan(int requested) {
        int applied = mFan.setFanLevel(requested);
        return applied == requested ? "Fan set to " + applied : "Fan set to the closest level, " + applied;
    }
}
