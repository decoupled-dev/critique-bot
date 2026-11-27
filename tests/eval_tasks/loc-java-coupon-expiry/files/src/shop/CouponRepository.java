package shop;

import java.time.LocalDate;
import java.util.HashMap;
import java.util.Map;

/** Coupons known to the shop, by code. */
public class CouponRepository {
    private final Map<String, Coupon> byCode = new HashMap<>();

    public void add(Coupon coupon) {
        byCode.put(coupon.getCode().toUpperCase(), coupon);
    }

    public Coupon find(String code) {
        if (code == null) {
            return null;
        }
        return byCode.get(code.trim().toUpperCase());
    }

    public static CouponRepository withDefaults(LocalDate today) {
        CouponRepository repository = new CouponRepository();
        repository.add(new Coupon("WELCOME10", 10, today.plusDays(30)));
        repository.add(new Coupon("SPRING20", 20, today));
        return repository;
    }
}
