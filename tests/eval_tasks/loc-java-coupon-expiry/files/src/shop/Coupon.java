package shop;

import java.time.LocalDate;

/** A discount code with a percentage and the last day it can be used. */
public class Coupon {
    private final String code;
    private final int percent;
    private final LocalDate expiry;

    public Coupon(String code, int percent, LocalDate expiry) {
        this.code = code;
        this.percent = percent;
        this.expiry = expiry;
    }

    public String getCode() {
        return code;
    }

    public int getPercent() {
        return percent;
    }

    public LocalDate getExpiry() {
        return expiry;
    }
}
