package ledger.io;

import ledger.model.Transaction;

import java.io.IOException;
import java.io.Reader;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;

/** Imports rows of date,payee,amount,note with a header row. */
public final class TransactionImporter {
    public static final class Result {
        public final List<Transaction> transactions = new ArrayList<>();
        public final List<String> rejected = new ArrayList<>();
    }

    public Result importFrom(Reader in) throws IOException, CsvFormatException {
        List<List<String>> rows = CsvReader.readAll(in);
        Result r = new Result();
        for (int i = 1; i < rows.size(); i++) {
            List<String> row = rows.get(i);
            if (row.size() != 4) {
                r.rejected.add("record " + i + ": expected 4 fields, got " + row.size());
                continue;
            }
            try {
                r.transactions.add(new Transaction(
                        LocalDate.parse(row.get(0).trim()),
                        row.get(1).trim(),
                        new BigDecimal(row.get(2).trim()),
                        row.get(3)));
            } catch (DateTimeParseException | NumberFormatException e) {
                r.rejected.add("record " + i + ": " + e.getMessage());
            }
        }
        return r;
    }
}
