// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Compiler-floor proof fixture.
//!
//! `slice::array_windows` was stabilised in Rust 1.94, so this crate compiles
//! only with a compiler at or above the snapshot floor. The proof is named so
//! a transcript validator can require that exact harness to run. The pure
//! Rust 1.93 control lives in `../compiler_floor/negative_1_93.rs`.

/// Return the adjacent pairs of a slice.
fn adjacent_pairs(values: &[u32]) -> Vec<(u32, u32)> {
    values
        .array_windows::<2>()
        .map(|pair| (pair[0], pair[1]))
        .collect()
}

#[kani::proof]
#[kani::unwind(4)]
fn bounded_adjacent_pairs_are_exact() {
    let values: [u32; 3] = [1, 2, 3];
    let pairs = adjacent_pairs(&values);
    kani::assert(pairs.len() == 2, "two adjacent pairs");
    kani::assert(pairs[0] == (1, 2), "first pair");
    kani::assert(pairs[1] == (2, 3), "second pair");
}