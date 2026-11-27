package shop;

import java.math.BigDecimal;
import java.math.RoundingMode;

/** Amount in euro cents, kept as BigDecimal with two decimals. */
public final class Money implements Comparable<Money> {
    public static final Money ZERO = new Money(BigDecimal.ZERO);
    private final BigDecimal amount;

    private Money(BigDecimal amount) {
        this.amount = amount.setScale(2, RoundingMode.HALF_EVEN);
    }

    public static Money of(String amount) {
        return new Money(new BigDecimal(amount));
    }

    public Money plus(Money other) {
        return new Money(amount.add(other.amount));
    }

    public Money minus(Money other) {
        BigDecimal r = amount.subtract(other.amount);
        return new Money(r.signum() < 0 ? BigDecimal.ZERO : r);
    }

    public Money times(int n) {
        return new Money(amount.multiply(BigDecimal.valueOf(n)));
    }

    public Money percent(int pct) {
        return new Money(amount.multiply(BigDecimal.valueOf(pct)).divide(BigDecimal.valueOf(100), 2, RoundingMode.HALF_EVEN));
    }

    @Override
    public int compareTo(Money o) {
        return amount.compareTo(o.amount);
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof Money && amount.compareTo(((Money) o).amount) == 0;
    }

    @Override
    public int hashCode() {
        return amount.stripTrailingZeros().hashCode();
    }

    @Override
    public String toString() {
        return "EUR " + amount.toPlainString();
    }
}
