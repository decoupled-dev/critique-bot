//! Small helpers for printing aligned plain-text tables.

pub mod pad;
pub mod table;
pub mod width;
pub mod wrap;

pub use pad::{pad_left, pad_right};
pub use table::Table;
pub use width::display_width;
pub use wrap::wrap;
