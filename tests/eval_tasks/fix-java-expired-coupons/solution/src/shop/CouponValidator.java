package shop;

import java.time.Instant;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/** Looks up coupon codes and decides whether one may be used on a cart. */
public final class CouponValidator {
    private final Map<String, Coupon> coupons = new HashMap<>();

    public CouponValidator register(Coupon coupon) {
        coupons.put(coupon.code().toUpperCase(Locale.ROOT), coupon);
        return this;
    }

    public Coupon validate(String code, Cart cart, Instant now) throws CouponRejected {
        Coupon coupon = coupons.get(code.trim().toUpperCase(Locale.ROOT));
        if (coupon == null) {
            throw new CouponRejected(CouponRejected.Reason.UNKNOWN_CODE, "unknown coupon " + code);
        }
        if (!isActive(coupon, now)) {
            throw new CouponRejected(CouponRejected.Reason.NOT_ACTIVE, coupon.code() + " is not active");
        }
        if (cart.subtotal().compareTo(coupon.minimumSpend()) < 0) {
            throw new CouponRejected(CouponRejected.Reason.BELOW_MINIMUM,
                    coupon.code() + " needs a minimum spend of " + coupon.minimumSpend());
        }
        return coupon;
    }

    static boolean isActive(Coupon coupon, Instant now) {
        boolean started = !now.isBefore(coupon.startsAt());
        boolean notExpired = now.isBefore(coupon.expiresAt());
        return started && notExpired;
    }
}
