/// Splits CSV text into rows of fields. Fields may be quoted with double quotes,
/// and a quoted field may contain commas.
pub fn read_rows(text: &str) -> Vec<Vec<String>> {
    text.lines()
        .filter(|line| !line.trim().is_empty())
        .map(split_fields)
        .collect()
}

fn split_fields(line: &str) -> Vec<String> {
    line.split(',')
        .map(|field| unquote(field.trim()))
        .collect()
}

fn unquote(field: &str) -> String {
    if field.len() >= 2 && field.starts_with('"') && field.ends_with('"') {
        field[1..field.len() - 1].replace("\"\"", "\"")
    } else {
        field.to_string()
    }
}
