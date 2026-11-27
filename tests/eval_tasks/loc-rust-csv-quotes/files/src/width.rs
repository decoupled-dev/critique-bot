/// Display width of a cell: characters, not bytes.
pub fn width(cell: &str) -> usize {
    cell.chars().count()
}

/// The widest cell of each column.
pub fn column_widths(rows: &[Vec<String>]) -> Vec<usize> {
    let columns = rows.iter().map(|row| row.len()).max().unwrap_or(0);
    (0..columns)
        .map(|col| rows.iter().filter_map(|row| row.get(col)).map(|c| width(c)).max().unwrap_or(0))
        .collect()
}
