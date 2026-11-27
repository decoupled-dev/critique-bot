package com.android.car.power;


public final class Mgr4 {
    public static final int AUDIO = 1;
    public static final int MEDIA = 2;
    public static final int DISPLAY = 3;
    public static final int BLUETOOTH = 4;
    public static final int WIFI = 5;
    public static final int VOICE_INTERACTION = 6;

    private Mgr4() {}

    public static String name(int a1) {
        switch (a1) {
            case AUDIO: return "AUDIO";
            case MEDIA: return "MEDIA";
            case DISPLAY: return "DISPLAY";
            case BLUETOOTH: return "BLUETOOTH";
            case WIFI: return "WIFI";
            case VOICE_INTERACTION: return "VOICE_INTERACTION";
            default: return "UNKNOWN(" + a1 + ")";
        }
    }
}
