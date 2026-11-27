package shop;

public final class CouponRejected extends Exception {
    public enum Reason { UNKNOWN_CODE, NOT_ACTIVE, BELOW_MINIMUM }

    private final Reason reason;

    public CouponRejected(Reason reason, String message) {
        super(message);
        this.reason = reason;
    }

    public Reason reason() {
        return reason;
    }
}
