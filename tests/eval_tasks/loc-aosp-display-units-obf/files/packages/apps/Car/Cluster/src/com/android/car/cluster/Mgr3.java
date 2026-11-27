package com.android.car.cluster;

/** Unit systems the cluster can show. */
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
