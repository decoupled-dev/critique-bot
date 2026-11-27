use crate::width::{column_widths, width};

/// Renders rows as an aligned text table.
pub fn render(rows: &[Vec<String>]) -> String {
    let widths = column_widths(rows);
    let mut out = String::new();
    for row in rows {
        let cells: Vec<String> = row
            .iter()
            .enumerate()
            .map(|(col, cell)| format!("{}{}", cell, " ".repeat(widths[col] - width(cell))))
            .collect();
        out.push_str(cells.join(" | ").trim_end());
        out.push('\n');
    }
    out
}
