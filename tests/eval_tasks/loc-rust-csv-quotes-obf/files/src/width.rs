/// Display width of a cell: characters, not bytes.
pub fn width(a1: &str) -> usize {
    a1.chars().count()
}

/// The widest cell of each column.
pub fn doIt1(a7: &[Vec<String>]) -> Vec<usize> {
    let a4 = a7.iter().map(|a6| a6.len()).max().unwrap_or(0);
    (0..a4)
        .map(|a3| a7.iter().filter_map(|a6| a6.get(a3)).map(|c| width(c)).max().unwrap_or(0))
        .collect()
}
