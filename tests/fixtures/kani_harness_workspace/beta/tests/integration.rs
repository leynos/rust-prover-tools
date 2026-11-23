//! Fixture integration test target.

#[kani::proof]
fn integration_proof() {}

/// A proof that only exists when the feature is enabled.
#[cfg(feature = "gated")]
#[kani::proof]
fn gated_proof() {}
