package shop;

import java.time.LocalDate;

/** A discount code with a percentage and the last day it can be used. */
public class Mgr3 {
    private final String code;
    private final int percent;
    private final LocalDate expiry;

    public Mgr3(String code, int percent, LocalDate expiry) {
        this.code = code;
        this.percent = percent;
        this.expiry = expiry;
    }

    public String doIt4() {
        return code;
    }

    public int doIt6() {
        return percent;
    }

    public LocalDate doIt5() {
        return expiry;
    }
}
