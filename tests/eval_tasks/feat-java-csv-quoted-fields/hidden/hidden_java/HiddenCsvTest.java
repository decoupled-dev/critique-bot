package ledger.io;

import java.io.StringReader;
import java.util.Arrays;
import java.util.List;

import ledger.model.Transaction;

public final class HiddenCsvTest {
    private static int failures;

    private static void check(boolean ok, String what) {
        System.out.println((ok ? "ok   " : "FAIL ") + what);
        if (!ok) failures++;
    }

    private static List<String> line(String s) {
        try {
            return CsvReader.parseLine(s);
        } catch (Exception e) {
            return Arrays.asList("<threw " + e + ">");
        }
    }

    private static List<List<String>> all(String s) {
        try {
            return CsvReader.readAll(new StringReader(s));
        } catch (Exception e) {
            return Arrays.asList(Arrays.asList("<threw " + e + ">"));
        }
    }

    public static void main(String[] args) throws Exception {
        check(line("a,b,c").equals(Arrays.asList("a", "b", "c")), "plain fields");
        check(line("a,,c,").equals(Arrays.asList("a", "", "c", "")), "empty fields");
        check(line("x,y\r").equals(Arrays.asList("x", "y")), "trailing CR trimmed");
        check(line("1,\"Smith, John\",3").equals(Arrays.asList("1", "Smith, John", "3")), "comma inside quotes");
        check(line("\"say \"\"hi\"\"\",2").equals(Arrays.asList("say \"hi\"", "2")), "doubled quotes");
        check(line("\"\",x").equals(Arrays.asList("", "x")), "empty quoted field");
        check(line("\"a\",\"b\"\r").equals(Arrays.asList("a", "b")), "quoted fields then CR");
        check(line("\"\"\"\"").equals(Arrays.asList("\"")), "field that is one quote");

        List<List<String>> rows = all("h1,h2\r\n\"multi\nline\",2\r\nlast,\"x\r\ny\"\n");
        check(rows.size() == 3, "records counted across line breaks: " + rows);
        check(rows.size() == 3 && rows.get(1).equals(Arrays.asList("multi\nline", "2")), "LF inside quotes");
        check(rows.size() == 3 && rows.get(2).equals(Arrays.asList("last", "x\ny")) ||
              rows.size() == 3 && rows.get(2).equals(Arrays.asList("last", "x\r\ny")), "CRLF inside quotes");

        List<List<String>> blank = all("a,b\n\n\"c\n\nd\",e\n");
        check(blank.size() == 2 && blank.get(1).equals(Arrays.asList("c\n\nd", "e")), "blank line inside quotes kept");

        try {
            CsvReader.readAll(new StringReader("a,b\nc,d\n\"open,e\nf\n"));
            check(false, "unterminated quote throws");
        } catch (CsvFormatException e) {
            check(e.getMessage().contains("line 3"), "unterminated quote reports start line: " + e.getMessage());
        }
        try {
            CsvReader.parseLine("a,\"b");
            check(false, "parseLine unterminated quote throws");
        } catch (CsvFormatException e) {
            check(true, "parseLine unterminated quote throws");
        }

        String csv = "date,payee,amount,note\n"
                + "2024-03-01,Grocer,-23.50,\n"
                + "2024-03-02,\"Smith, John\",150.00,\"rent share\"\n"
                + "2024-03-04,\"Utility Co\",-80.00,\"bill for\nFebruary\"\n";
        TransactionImporter.Result r = new TransactionImporter().importFrom(new StringReader(csv));
        check(r.rejected.isEmpty(), "importer accepts quoted rows " + r.rejected);
        check(r.transactions.size() == 3, "three transactions");
        if (r.transactions.size() == 3) {
            Transaction t = r.transactions.get(1);
            check(t.payee().equals("Smith, John") && t.amount().toPlainString().equals("150.00"), "payee with comma");
            check(r.transactions.get(2).note().equals("bill for\nFebruary"), "multi-line note");
        }

        if (failures > 0) {
            System.out.println(failures + " failure(s)");
            System.exit(1);
        }
    }
}
