package ledger.io;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.Reader;
import java.util.ArrayList;
import java.util.List;

/** Minimal CSV reader: comma separated, one record per line. */
public final class CsvReader {
    private CsvReader() {}

    /** Split one line into fields. */
    public static List<String> parseLine(String line) throws CsvFormatException {
        if (line.endsWith("\r")) {
            line = line.substring(0, line.length() - 1);
        }
        List<String> fields = new ArrayList<>();
        int start = 0;
        while (true) {
            int comma = line.indexOf(',', start);
            if (comma < 0) {
                fields.add(line.substring(start));
                return fields;
            }
            fields.add(line.substring(start, comma));
            start = comma + 1;
        }
    }

    /** Read every record. Blank lines are skipped. */
    public static List<List<String>> readAll(Reader in) throws IOException, CsvFormatException {
        BufferedReader br = new BufferedReader(in);
        List<List<String>> rows = new ArrayList<>();
        String line;
        while ((line = br.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            rows.add(parseLine(line));
        }
        return rows;
    }
}
