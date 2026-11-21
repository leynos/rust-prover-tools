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
