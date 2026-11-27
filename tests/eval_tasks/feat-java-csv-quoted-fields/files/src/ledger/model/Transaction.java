package ledger.model;

import java.math.BigDecimal;
import java.time.LocalDate;

public final class Transaction {
    private final LocalDate date;
    private final String payee;
    private final BigDecimal amount;
    private final String note;

    public Transaction(LocalDate date, String payee, BigDecimal amount, String note) {
        this.date = date;
        this.payee = payee;
        this.amount = amount;
        this.note = note;
    }

    public LocalDate date() { return date; }
    public String payee() { return payee; }
    public BigDecimal amount() { return amount; }
    public String note() { return note; }

    @Override
    public String toString() {
        return date + " " + payee + " " + amount.toPlainString() + (note.isEmpty() ? "" : " (" + note + ")");
    }
}
