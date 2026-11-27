/// Number of terminal columns `s` takes (one per character; no wide/zero-width handling).
pub fn display_width(s: &str) -> usize {
    s.chars().count()
}
