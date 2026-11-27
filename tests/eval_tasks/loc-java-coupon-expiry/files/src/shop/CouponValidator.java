package shop;

import java.time.LocalDate;

/** Decides whether a coupon may still be applied. */
public class CouponValidator {
    private final Clock clock;

    public CouponValidator(Clock clock) {
        this.clock = clock;
    }

    public boolean isUsable(Coupon coupon, int cartTotalCents) {
        if (coupon == null || cartTotalCents <= 0) {
            return false;
        }
        return !isExpired(coupon) && coupon.getPercent() > 0 && coupon.getPercent() <= 90;
    }

    boolean isExpired(Coupon coupon) {
        LocalDate today = clock.today();
        // a coupon is valid through the end of its expiry day
        return !today.isBefore(coupon.getExpiry());
    }

    public interface Clock {
        LocalDate today();
    }
}
