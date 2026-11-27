package shop;

/** Totals in cents, with rounding that never favours the shop. */
public class PriceCalculator {
    public int discounted(int totalCents, int percent) {
        long off = (long) totalCents * percent / 100;
        return (int) (totalCents - off);
    }

    public int withTax(int cents, int taxPermille) {
        return (int) Math.round(cents * (1000 + taxPermille) / 1000.0);
    }
}
