package shop;

import java.time.LocalDate;
import java.util.HashMap;
import java.util.Map;

/** Coupons known to the shop, by code. */
public class Mgr4 {
    private final Map<String, Mgr3> a2 = new HashMap<>();

    public void add(Mgr3 a5) {
        a2.put(a5.doIt4().toUpperCase(), a5);
    }

    public Mgr3 find(String code) {
        if (code == null) {
            return null;
        }
        return a2.get(code.trim().toUpperCase());
    }

    public static Mgr4 doIt10(LocalDate a11) {
        Mgr4 a8 = new Mgr4();
        a8.add(new Mgr3("WELCOME10", 10, a11.plusDays(30)));
        a8.add(new Mgr3("SPRING20", 20, a11));
        return a8;
    }
}
