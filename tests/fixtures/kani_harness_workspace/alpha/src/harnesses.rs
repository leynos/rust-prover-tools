//! Proofs reached through a plain module declaration.

/// A normal proof.
#[kani::proof]
pub fn verified() {}

/// A proof with its own unwinding bound.
#[kani::proof]
#[kani::unwind(2)]
pub fn bounded() {}

/// A proof that only exists when the feature is enabled.
#[cfg(feature = "extra")]
#[kani::proof]
pub fn conditional_extra() {}

pub mod nested {
    #[kani::proof]
    fn proof() {}
}

/// A deliberately false assertion, so a live run has a failing harness.
#[kani::proof]
pub fn failing() {
    assert!(1 + 1 == 3, "the fixture's seeded failure");
}
