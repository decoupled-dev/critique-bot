package shop;

import java.time.Clock;
import java.time.Instant;

/** Totals a cart, applying at most one coupon. */
public final class Checkout {
    public static final class Receipt {
        private final Money subtotal;
        private final Money discount;
        private final Money total;
        private final String couponCode;
        private final String note;

        Receipt(Money subtotal, Money discount, Money total, String couponCode, String note) {
            this.subtotal = subtotal;
            this.discount = discount;
            this.total = total;
            this.couponCode = couponCode;
            this.note = note;
        }

        public Money subtotal() { return subtotal; }
        public Money discount() { return discount; }
        public Money total() { return total; }
        public String couponCode() { return couponCode; }
        public String note() { return note; }
    }

    private final CouponValidator validator;
    private final Clock clock;

    public Checkout(CouponValidator validator, Clock clock) {
        this.validator = validator;
        this.clock = clock;
    }

    public Receipt total(Cart cart, String couponCode) {
        Money subtotal = cart.subtotal();
        if (couponCode == null || couponCode.trim().isEmpty()) {
            return new Receipt(subtotal, Money.ZERO, subtotal, null, "");
        }
        Instant now = clock.instant();
        try {
            Coupon coupon = validator.validate(couponCode, cart, now);
            Money discount = coupon.discountFor(subtotal);
            return new Receipt(subtotal, discount, subtotal.minus(discount), coupon.code(), "");
        } catch (CouponRejected e) {
            return new Receipt(subtotal, Money.ZERO, subtotal, null, e.getMessage());
        }
    }
}
