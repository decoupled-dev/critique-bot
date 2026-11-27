package com.android.car.cluster;

/** Unit systems the cluster can show. */
public enum Units {
    METRIC,
    IMPERIAL;

    static Units parse(String value) {
        if ("imperial".equalsIgnoreCase(value)) {
            return IMPERIAL;
        }
        return METRIC;
    }
}
