// Copyright Kani Contributors
// SPDX-License-Identifier: Apache-2.0 OR MIT

//! Upstream `tests/cargo-kani/simple-lib`, with its `pair` module flattened
//! into this file. The harness is the upstream one, unchanged.

/// A pair of unsigned values.
pub struct Pair {
    first: u64,
    second: u64,
}

impl Pair {
    /// Build a pair.
    #[must_use]
    pub fn new(first: u64, second: u64) -> Self {
        Self { first, second }
    }

    /// Return the wrapping sum.
    #[must_use]
    pub fn sum(&self) -> u64 {
        self.first.wrapping_add(self.second)
    }
}

#[cfg(kani)]
mod kani_tests {
    use super::Pair;

    #[kani::proof]
    fn test_sum() {
        let a: u64 = kani::any();
        let b: u64 = kani::any();
        let p = Pair::new(a, b);
        assert!(p.sum() == a.wrapping_add(b));
    }
}