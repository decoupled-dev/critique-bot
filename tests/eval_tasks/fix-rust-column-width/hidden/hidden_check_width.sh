#!/bin/sh
set -e
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
rustc --edition 2021 --crate-type lib --crate-name colfmt -o "$out/libcolfmt.rlib" src/lib.rs
rustc --edition 2021 --test -o "$out/hidden_width" --extern colfmt="$out/libcolfmt.rlib" hidden_rs/hidden_width.rs
"$out/hidden_width" -q
