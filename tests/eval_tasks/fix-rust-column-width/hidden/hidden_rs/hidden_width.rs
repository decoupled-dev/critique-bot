use colfmt::table::Align;
use colfmt::{display_width, pad_left, pad_right, wrap, Table};

#[test]
fn width_counts_characters() {
    assert_eq!(display_width("José"), 4);
    assert_eq!(display_width("Zoë"), 3);
    assert_eq!(display_width("Müller"), 6);
    assert_eq!(display_width("abc"), 3);
    assert_eq!(display_width(""), 0);
}

#[test]
fn padding_non_ascii() {
    assert_eq!(pad_right("Zoë", 5), "Zoë  ");
    assert_eq!(pad_left("€5", 4), "  €5");
    assert_eq!(pad_right("Müller", 3), "Müller");
}

#[test]
fn table_rows_line_up() {
    let mut t = Table::new(&["name", "city", "qty"]).align(2, Align::Right);
    t.row(&["José", "Köln", "3"]);
    t.row(&["Bob", "Oslo", "12"]);
    t.row(&["Zoë Müller", "Århus", "7"]);
    let text = t.render();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], "name       | city  | qty");
    assert_eq!(lines[2], "José       | Köln  |   3");
    assert_eq!(lines[3], "Bob        | Oslo  |  12");
    assert_eq!(lines[4], "Zoë Müller | Århus |   7");
    let w: Vec<usize> = lines.iter().map(|l| l.chars().count()).collect();
    assert!(w.iter().all(|x| *x == w[0]), "{:?}", w);
}

#[test]
fn wrap_non_ascii() {
    assert_eq!(wrap("café crème brûlée très bon", 10), vec!["café crème", "brûlée", "très bon"]);
    assert_eq!(wrap("ÅÅÅ ÄÄÄ", 7), vec!["ÅÅÅ ÄÄÄ"]);
    assert_eq!(wrap("the quick brown fox jumps", 10), vec!["the quick", "brown fox", "jumps"]);
}
