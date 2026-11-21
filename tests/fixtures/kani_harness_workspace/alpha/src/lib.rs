//! Fixture library for inventory discovery.
//!
//! The comment mentions #[kani::proof] and `mod fake;` on purpose: neither is
//! code, and discovery must not read it as such.

#[cfg(kani)]
mod harnesses;

#[path = "extra/path_proofs.rs"]
mod path_proofs;

pub mod inline {
    #[kani::proof]
    fn inside() {}
}

pub mod shared {
    #[kani::proof]
    fn proof() {}
}
