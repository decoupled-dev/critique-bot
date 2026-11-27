package com.android.car.power;

/** Power components a policy can turn on or off. */
public final class PowerComponent {
    public static final int AUDIO = 1;
    public static final int MEDIA = 2;
    public static final int DISPLAY = 3;
    public static final int BLUETOOTH = 4;
    public static final int WIFI = 5;
    public static final int VOICE_INTERACTION = 6;

    private PowerComponent() {}

    public static String name(int component) {
        switch (component) {
            case AUDIO: return "AUDIO";
            case MEDIA: return "MEDIA";
            case DISPLAY: return "DISPLAY";
            case BLUETOOTH: return "BLUETOOTH";
            case WIFI: return "WIFI";
            case VOICE_INTERACTION: return "VOICE_INTERACTION";
            default: return "UNKNOWN(" + component + ")";
        }
    }
}
