package com.example.hvac.common;

/** Drives the blower fan of one HVAC zone. Levels go from 0 (off) to MAX_LEVEL. */
public class FanController {
    public static final int MAX_LEVEL = 7;

    private final int mZone;
    private int mLevel;

    public FanController(int zone) {
        mZone = zone;
    }

    public int getZone() {
        return mZone;
    }

    /**
     * Sets the fan level for this zone, clamped to 0..MAX_LEVEL. Returns the level applied.
     * (Renamed from setSpeed: "speed" was confused with the vehicle speed.)
     */
    public int setFanLevel(int level) {
        mLevel = Math.max(0, Math.min(level, MAX_LEVEL));
        return mLevel;
    }

    public int getFanLevel() {
        return mLevel;
    }
}
