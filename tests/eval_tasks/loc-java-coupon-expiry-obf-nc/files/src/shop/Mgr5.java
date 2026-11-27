package shop;

import java.time.LocalDate;


public class Mgr5 {
    private final Mgr2 clock;

    public Mgr5(Mgr2 clock) {
        this.clock = clock;
    }

    public boolean doIt7(Mgr3 a5, int a3) {
        if (a5 == null || a3 <= 0) {
            return false;
        }
        return !isExpired(a5) && a5.doIt6() > 0 && a5.doIt6() <= 90;
    }

    boolean isExpired(Mgr3 a5) {
        LocalDate a11 = clock.a11();
        
        return !a11.isBefore(a5.doIt5());
    }

    public interface Mgr2 {
        LocalDate a11();
    }
}
