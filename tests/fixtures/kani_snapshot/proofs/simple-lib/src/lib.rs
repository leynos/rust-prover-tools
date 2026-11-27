// Copyright Kani Contributors
// SPDX-License-Identifier: Apache-2.0 OR MIT

//! Upstream `tests/cargo-kani/simple-lib`, copied from the pinned commit
//! `0d2328a93f0e0ff66132d6bfa1a7d884877cf862`, with a fixture documentation
//! comment added to this file and to `pair.rs`.
//!
//! It declares `pair` with `pub mod pair;` and re-exports `Pair`, then adds
//! the `test_sum` harness the bundle smoke runs.

pub mod pair;
pub use pair::Pair;

#[cfg(test)]
mod tests {
    #[test]
    fn it_works() {
        assert_eq!(2 + 2, 4);
    }
}

#[cfg(kani)]
mod kani_tests {
    use super::*;

    #[kani::proof]
    fn test_sum() {
        let a: u64 = kani::any();
        let b: u64 = kani::any();
        let p = Pair::new(a, b);
        assert!(p.sum() == a.wrapping_add(b));
    }
}
