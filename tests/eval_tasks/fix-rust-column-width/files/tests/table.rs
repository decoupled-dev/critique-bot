use colfmt::table::Align;
use colfmt::{pad_right, wrap, Table};

#[test]
fn ascii_table_aligned() {
    let mut t = Table::new(&["name", "qty"]).align(1, Align::Right);
    t.row(&["bolts", "120"]);
    t.row(&["nut", "7"]);
    assert_eq!(t.render(), "name  | qty\n------+----\nbolts | 120\nnut   |   7");
}

#[test]
fn pad_ascii() {
    assert_eq!(pad_right("ab", 4), "ab  ");
    assert_eq!(pad_right("abcdef", 4), "abcdef");
}

#[test]
fn wrap_ascii() {
    assert_eq!(wrap("the quick brown fox jumps", 10), vec!["the quick", "brown fox", "jumps"]);
}
