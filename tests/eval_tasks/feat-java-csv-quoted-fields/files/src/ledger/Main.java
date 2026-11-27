package ledger;

import ledger.io.TransactionImporter;
import ledger.model.Transaction;

import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

public final class Main {
    public static void main(String[] args) throws Exception {
        try (Reader in = Files.newBufferedReader(Paths.get(args[0]), StandardCharsets.UTF_8)) {
            TransactionImporter.Result r = new TransactionImporter().importFrom(in);
            for (Transaction t : r.transactions) System.out.println(t);
            for (String s : r.rejected) System.out.println("rejected " + s);
        }
    }
}
