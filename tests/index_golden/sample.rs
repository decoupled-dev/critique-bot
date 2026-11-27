use crate::layout::width;
mod table;

pub struct Column {
    title: String,
}

pub trait Render {
    fn render(&self) -> String;
}

impl Render for Column {
    fn render(&self) -> String {
        let w = width(&self.title);
        format!("{:w$}", self.title, w = w)
    }
}

pub fn widest(cols: &[Column]) -> usize {
    cols.iter().map(|c| width(&c.title)).max().expect("no columns")
}
