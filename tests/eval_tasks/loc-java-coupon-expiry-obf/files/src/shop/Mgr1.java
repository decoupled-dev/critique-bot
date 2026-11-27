package shop;

import java.util.ArrayList;
import java.util.List;

/** The checkout cart. */
public class Mgr1 {
    private final List<Integer> items = new ArrayList<>();
    private final Mgr4 coupons;
    private final Mgr5 validator;
    private final Mgr6 a7 = new Mgr6();
    private Mgr3 a1;

    public Mgr1(Mgr4 coupons, Mgr5 validator) {
        this.coupons = coupons;
        this.validator = validator;
    }

    public void doIt1(int a4) {
        items.add(a4);
    }

    public int doIt8() {
        int a9 = 0;
        for (int a4 : items) {
            a9 += a4;
        }
        return a9;
    }

    public String doIt2(String code) {
        Mgr3 a5 = coupons.find(code);
        if (a5 == null) {
            return "unknown coupon";
        }
        if (!validator.doIt7(a5, doIt8())) {
            return "coupon expired";
        }
        a1 = a5;
        return "ok";
    }

    public int doIt9() {
        int a9 = doIt8();
        return a1 == null ? a9 : a7.doIt3(a9, a1.doIt6());
    }
}
