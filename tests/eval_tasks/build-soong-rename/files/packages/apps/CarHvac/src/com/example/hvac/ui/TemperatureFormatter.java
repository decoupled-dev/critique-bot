package com.example.hvac.ui;

/** Formats a set point for the panel. */
public final class TemperatureFormatter {
    private TemperatureFormatter() {}

    public static String format(float celsius, boolean fahrenheit) {
        if (fahrenheit) {
            return Math.round(celsius * 9 / 5 + 32) + "°F";
        }
        return String.format(java.util.Locale.ROOT, "%.1f°C", celsius);
    }
}
