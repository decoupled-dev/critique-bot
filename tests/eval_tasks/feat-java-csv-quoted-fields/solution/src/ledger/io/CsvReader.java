package ledger.io;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.Reader;
import java.util.ArrayList;
import java.util.List;

/**
 * Small CSV reader: comma separated fields, optionally wrapped in double quotes.
 * Inside quotes, commas and line breaks are literal and {@code ""} is one quote.
 */
public final class CsvReader {
    private CsvReader() {}

    /** Split one line into fields. */
    public static List<String> parseLine(String line) throws CsvFormatException {
        List<String> fields = new ArrayList<>();
        StringBuilder field = new StringBuilder();
        if (parse(line, 0, fields, field, false) != null) {
            throw new CsvFormatException("line 1: unterminated quoted field");
        }
        return fields;
    }

    /**
     * Parse {@code text}, appending finished fields to {@code fields}. Returns null when the
     * record is complete, or the partial field when the text ended inside quotes.
     */
    private static StringBuilder parse(String text, int from, List<String> fields, StringBuilder field,
                                       boolean inQuotes) {
        if (!inQuotes && text.endsWith("\r")) {
            text = text.substring(0, text.length() - 1);
        }
        int i = from;
        boolean quotedField = inQuotes;
        while (i < text.length()) {
            char c = text.charAt(i);
            if (inQuotes) {
                if (c == '"') {
                    if (i + 1 < text.length() && text.charAt(i + 1) == '"') {
                        field.append('"');
                        i += 2;
                        continue;
                    }
                    inQuotes = false;
                    i++;
                    continue;
                }
                field.append(c);
                i++;
                continue;
            }
            if (c == ',') {
                fields.add(field.toString());
                field.setLength(0);
                quotedField = false;
                i++;
                continue;
            }
            if (c == '"' && field.length() == 0 && !quotedField) {
                inQuotes = true;
                quotedField = true;
                i++;
                continue;
            }
            field.append(c);
            i++;
        }
        if (inQuotes) {
            return field;
        }
        if (text.endsWith("\r") && field.length() > 0 && field.charAt(field.length() - 1) == '\r') {
            field.setLength(field.length() - 1);
        }
        fields.add(field.toString());
        field.setLength(0);
        return null;
    }

    /** Read every record. Blank lines between records are skipped. */
    public static List<List<String>> readAll(Reader in) throws IOException, CsvFormatException {
        BufferedReader br = new BufferedReader(in);
        List<List<String>> rows = new ArrayList<>();
        String line;
        int lineNo = 0;
        while ((line = br.readLine()) != null) {
            lineNo++;
            if (line.trim().isEmpty()) {
                continue;
            }
            int startLine = lineNo;
            List<String> fields = new ArrayList<>();
            StringBuilder pending = parse(line, 0, fields, new StringBuilder(), false);
            while (pending != null) {
                String next = br.readLine();
                if (next == null) {
                    throw new CsvFormatException("line " + startLine + ": unterminated quoted field");
                }
                lineNo++;
                pending.append('\n');
                pending = parse(next, 0, fields, pending, true);
            }
            rows.add(fields);
        }
        return rows;
    }
}
