// Copyright Kani Contributors
// SPDX-License-Identifier: Apache-2.0 OR MIT

//! The public `Pair` tuple struct used by this fixture's harnesses.
//!
//! It defines `Pair`, its `new` constructor and its wrapping `sum` method.
//! `lib.rs` declares it with `pub mod pair;` and re-exports `Pair`. The body
//! is an upstream copy from the pinned commit
//! `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`; only this header is added.

pub struct Pair(pub u64, pub u64);

impl Pair {
    pub fn new(a: u64, b: u64) -> Self {
        Pair(a, b)
    }
    pub fn sum(&self) -> u64 {
        self.0.wrapping_add(self.1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn one_plus_two() {
        let p = Pair::new(1, 2);
        assert_eq!(p.sum(), 3);
    }
}

#[cfg(kani)]
mod kani_tests {
    use super::*;

    #[kani::proof]
    fn test_one_plus_two() {
        let p = Pair::new(1, 2);
        assert!(p.sum() == 3);
    }
}
