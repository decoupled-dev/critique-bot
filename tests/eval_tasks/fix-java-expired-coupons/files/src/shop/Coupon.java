package shop;

import java.time.Instant;

/**
 * A promotional code. {@code startsAt} is inclusive, {@code expiresAt} is exclusive.
 * Either a percentage or a fixed amount off; {@code minimumSpend} may be zero.
 */
public final class Coupon {
    private final String code;
    private final Instant startsAt;
    private final Instant expiresAt;
    private final int percentOff;
    private final Money amountOff;
    private final Money minimumSpend;

    public Coupon(String code, Instant startsAt, Instant expiresAt,
                  int percentOff, Money amountOff, Money minimumSpend) {
        this.code = code;
        this.startsAt = startsAt;
        this.expiresAt = expiresAt;
        this.percentOff = percentOff;
        this.amountOff = amountOff;
        this.minimumSpend = minimumSpend;
    }

    public static Coupon percent(String code, Instant startsAt, Instant expiresAt, int pct, String minimumSpend) {
        return new Coupon(code, startsAt, expiresAt, pct, Money.ZERO, Money.of(minimumSpend));
    }

    public static Coupon fixed(String code, Instant startsAt, Instant expiresAt, String amountOff, String minimumSpend) {
        return new Coupon(code, startsAt, expiresAt, 0, Money.of(amountOff), Money.of(minimumSpend));
    }

    public String code() { return code; }
    public Instant startsAt() { return startsAt; }
    public Instant expiresAt() { return expiresAt; }
    public Money minimumSpend() { return minimumSpend; }

    public Money discountFor(Money subtotal) {
        if (percentOff > 0) {
            return subtotal.percent(percentOff);
        }
        return amountOff.compareTo(subtotal) > 0 ? subtotal : amountOff;
    }
}
