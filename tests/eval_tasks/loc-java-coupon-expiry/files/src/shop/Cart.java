package shop;

import java.util.ArrayList;
import java.util.List;

/** The checkout cart. */
public class Cart {
    private final List<Integer> items = new ArrayList<>();
    private final CouponRepository coupons;
    private final CouponValidator validator;
    private final PriceCalculator prices = new PriceCalculator();
    private Coupon applied;

    public Cart(CouponRepository coupons, CouponValidator validator) {
        this.coupons = coupons;
        this.validator = validator;
    }

    public void addItem(int cents) {
        items.add(cents);
    }

    public int subtotal() {
        int sum = 0;
        for (int cents : items) {
            sum += cents;
        }
        return sum;
    }

    public String applyCoupon(String code) {
        Coupon coupon = coupons.find(code);
        if (coupon == null) {
            return "unknown coupon";
        }
        if (!validator.isUsable(coupon, subtotal())) {
            return "coupon expired";
        }
        applied = coupon;
        return "ok";
    }

    public int total() {
        int sum = subtotal();
        return applied == null ? sum : prices.discounted(sum, applied.getPercent());
    }
}
