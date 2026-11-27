use crate::pad::{pad_left, pad_right};
use crate::width::display_width;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Align {
    Left,
    Right,
}

/// A table of string cells rendered with ` | ` between columns.
pub struct Table {
    headers: Vec<String>,
    align: Vec<Align>,
    rows: Vec<Vec<String>>,
}

impl Table {
    pub fn new(headers: &[&str]) -> Self {
        Table {
            headers: headers.iter().map(|h| h.to_string()).collect(),
            align: vec![Align::Left; headers.len()],
            rows: Vec::new(),
        }
    }

    pub fn align(mut self, col: usize, a: Align) -> Self {
        self.align[col] = a;
        self
    }

    pub fn row(&mut self, cells: &[&str]) {
        let mut r: Vec<String> = cells.iter().map(|c| c.to_string()).collect();
        r.resize(self.headers.len(), String::new());
        self.rows.push(r);
    }

    fn widths(&self) -> Vec<usize> {
        let mut w: Vec<usize> = self.headers.iter().map(|h| display_width(h)).collect();
        for r in &self.rows {
            for (i, c) in r.iter().enumerate() {
                w[i] = w[i].max(display_width(c));
            }
        }
        w
    }

    fn line(&self, cells: &[String], widths: &[usize]) -> String {
        let parts: Vec<String> = cells
            .iter()
            .enumerate()
            .map(|(i, c)| match self.align[i] {
                Align::Left => pad_right(c, widths[i]),
                Align::Right => pad_left(c, widths[i]),
            })
            .collect();
        parts.join(" | ").trim_end().to_string()
    }

    pub fn render(&self) -> String {
        let widths = self.widths();
        let mut out = vec![self.line(&self.headers, &widths)];
        out.push(widths.iter().map(|w| "-".repeat(*w)).collect::<Vec<_>>().join("-+-"));
        for r in &self.rows {
            out.push(self.line(r, &widths));
        }
        out.join("\n")
    }
}
