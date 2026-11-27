package shop;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

public final class CheckoutTest {
    private static int failures;

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "ok   " : "FAIL ") + what);
        if (!ok) failures++;
    }

    public static void main(String[] args) {
        Instant now = Instant.parse("2024-07-10T12:00:00Z");
        CouponValidator v = new CouponValidator()
                .register(Coupon.percent("SUMMER24", Instant.parse("2024-06-01T00:00:00Z"),
                        Instant.parse("2024-09-01T00:00:00Z"), 10, "0"))
                .register(Coupon.fixed("BIG5", Instant.parse("2024-01-01T00:00:00Z"),
                        Instant.parse("2025-01-01T00:00:00Z"), "5", "40"));
        Checkout checkout = new Checkout(v, Clock.fixed(now, ZoneOffset.UTC));
        Cart cart = new Cart().add("A-1", "19.99", 2).add("B-7", "5.00", 1);

        check(checkout.total(cart, null).total().equals(Money.of("44.98")), "no coupon");
        check(checkout.total(cart, "summer24").total().equals(Money.of("40.48")), "percent coupon, any case");
        check(checkout.total(cart, "NOPE").discount().equals(Money.ZERO), "unknown coupon ignored");
        Cart small = new Cart().add("B-7", "5.00", 3);
        check(checkout.total(small, "BIG5").discount().equals(Money.ZERO), "minimum spend enforced");
        check(checkout.total(cart, "BIG5").total().equals(Money.of("39.98")), "fixed coupon");

        if (failures > 0) {
            System.out.println(failures + " failure(s)");
            System.exit(1);
        }
    }
}
