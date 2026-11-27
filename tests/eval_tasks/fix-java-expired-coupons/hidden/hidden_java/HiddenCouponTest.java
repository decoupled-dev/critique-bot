package shop;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

public final class HiddenCouponTest {
    private static int failures;

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "ok   " : "FAIL ") + what);
        if (!ok) failures++;
    }

    private static Checkout at(String when, CouponValidator v) {
        return new Checkout(v, Clock.fixed(Instant.parse(when), ZoneOffset.UTC));
    }

    private static CouponRejected.Reason reason(CouponValidator v, String code, Cart cart, String when) {
        try {
            v.validate(code, cart, Instant.parse(when));
            return null;
        } catch (CouponRejected e) {
            return e.reason();
        }
    }

    public static void main(String[] args) {
        CouponValidator v = new CouponValidator()
                .register(Coupon.percent("SUMMER24", Instant.parse("2024-06-01T00:00:00Z"),
                        Instant.parse("2024-09-01T00:00:00Z"), 10, "0"))
                .register(Coupon.fixed("AUTUMN", Instant.parse("2024-10-01T00:00:00Z"),
                        Instant.parse("2024-11-01T00:00:00Z"), "5", "20"));
        Cart cart = new Cart().add("A-1", "19.99", 2).add("B-7", "5.00", 1);

        check(at("2024-07-10T12:00:00Z", v).total(cart, "SUMMER24").total().equals(Money.of("40.48")), "active coupon applies");
        check(at("2024-10-15T12:00:00Z", v).total(cart, "SUMMER24").discount().equals(Money.ZERO), "expired coupon gives no discount");
        check(at("2024-07-10T12:00:00Z", v).total(cart, "AUTUMN").discount().equals(Money.ZERO), "future coupon gives no discount");
        check(at("2024-10-15T12:00:00Z", v).total(cart, "AUTUMN").total().equals(Money.of("39.98")), "second coupon active in its window");
        check(at("2024-06-01T00:00:00Z", v).total(cart, "SUMMER24").discount().equals(Money.of("4.50")), "start instant is inclusive");
        check(at("2024-09-01T00:00:00Z", v).total(cart, "SUMMER24").discount().equals(Money.ZERO), "expiry instant is exclusive");
        check(at("2024-08-31T23:59:59Z", v).total(cart, "SUMMER24").discount().equals(Money.of("4.50")), "last second before expiry");
        check(reason(v, "SUMMER24", cart, "2025-01-01T00:00:00Z") == CouponRejected.Reason.NOT_ACTIVE, "expired -> NOT_ACTIVE");
        check(reason(v, "AUTUMN", cart, "2024-01-01T00:00:00Z") == CouponRejected.Reason.NOT_ACTIVE, "not started -> NOT_ACTIVE");
        Cart small = new Cart().add("B-7", "5.00", 2);
        check(reason(v, "AUTUMN", small, "2024-10-15T00:00:00Z") == CouponRejected.Reason.BELOW_MINIMUM, "minimum spend still enforced");
        check(reason(v, "nope", cart, "2024-10-15T00:00:00Z") == CouponRejected.Reason.UNKNOWN_CODE, "unknown code");
        check(at("2024-10-15T12:00:00Z", v).total(cart, "SUMMER24").note().contains("not active"), "receipt explains rejection");

        if (failures > 0) {
            System.out.println(failures + " failure(s)");
            System.exit(1);
        }
    }
}
