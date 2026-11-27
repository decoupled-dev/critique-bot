

pub fn read_rows(a8: &str) -> Vec<Vec<String>> {
    a8.lines()
        .filter(|a5| !a5.trim().is_empty())
        .map(doIt2)
        .collect()
}

fn doIt2(a5: &str) -> Vec<String> {
    a5.split(',')
        .map(|field| doIt3(field.trim()))
        .collect()
}

fn doIt3(field: &str) -> String {
    if field.len() >= 2 && field.starts_with('"') && field.ends_with('"') {
        field[1..field.len() - 1].replace("\"\"", "\"")
    } else {
        field.to_string()
    }
}
