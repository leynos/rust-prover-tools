// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Compiler-floor negative control.
//!
//! This is the same pure function without any Kani annotation or Cargo
//! `rust-version` gate. On Rust 1.93 it must fail because `array_windows` is
//! not yet available, which shows the positive fixture is genuinely gated on
//! the 1.94 stabilisation rather than on repository configuration.

/// Return the adjacent pairs of a slice.
fn adjacent_pairs(values: &[u32]) -> Vec<(u32, u32)> {
    values
        .array_windows::<2>()
        .map(|pair| (pair[0], pair[1]))
        .collect()
}

fn main() {
    let values: [u32; 3] = [1, 2, 3];
    assert_eq!(adjacent_pairs(&values), vec![(1, 2), (2, 3)]);
}