package com.android.car.cluster;


public enum Mgr3 {
    METRIC,
    IMPERIAL;

    static Mgr3 parse(String value) {
        if ("imperial".equalsIgnoreCase(value)) {
            return IMPERIAL;
        }
        return METRIC;
    }
}
