use crate::width::{doIt1, width};


pub fn render(a7: &[Vec<String>]) -> String {
    let a9 = doIt1(a7);
    let mut out = String::new();
    for a6 in a7 {
        let a2: Vec<String> = a6
            .iter()
            .enumerate()
            .map(|(a3, a1)| format!("{}{}", a1, " ".repeat(a9[a3] - width(a1))))
            .collect();
        out.push_str(a2.join(" | ").trim_end());
        out.push('\n');
    }
    out
}
