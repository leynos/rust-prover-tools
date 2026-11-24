// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Upstream `simple-kissat` bundle-smoke equivalent.

/// Sum a fixed three-element array.
fn bounded_sum(values: [u8; 3]) -> u8 {
    values[0] + values[1] + values[2]
}

#[kani::proof]
#[kani::solver(kissat)]
#[kani::unwind(2)]
fn kissat_proves_a_bounded_sum() {
    let values: [u8; 3] = [1, 2, 3];
    kani::assert(bounded_sum(values) == 6, "bounded sum");
}
