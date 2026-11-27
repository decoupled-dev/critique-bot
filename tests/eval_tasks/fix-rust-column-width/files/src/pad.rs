use crate::width::display_width;

/// `s` followed by spaces up to `width` columns. Longer strings are returned unchanged.
pub fn pad_right(s: &str, width: usize) -> String {
    let w = display_width(s);
    let mut out = String::from(s);
    for _ in w..width {
        out.push(' ');
    }
    out
}

/// Spaces then `s`, `width` columns in total. Longer strings are returned unchanged.
pub fn pad_left(s: &str, width: usize) -> String {
    let w = display_width(s);
    let mut out = String::new();
    for _ in w..width {
        out.push(' ');
    }
    out.push_str(s);
    out
}
