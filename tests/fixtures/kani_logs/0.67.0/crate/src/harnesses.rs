//! Independent positive and seeded negative subjects for Kani's output vocabulary.

/// A reachable boundary witnesses a saturating arithmetic property.
#[kani::proof]
fn verified() {
    let value = kani::any::<u8>();
    assert!(
        value.saturating_add(1) >= value,
        "saturation must not decrease a value"
    );
    kani::cover!(value == 254, "the boundary below saturation is reachable");
}

/// A full-domain symbolic value exposes the deliberately false upper bound.
#[kani::proof]
fn counterexample() {
    let value = kani::any::<u8>();
    assert!(value < 255, "the seeded bound incorrectly excludes 255");
}

/// The assertion passes while its requested witness is unreachable.
#[kani::proof]
fn unsatisfied_cover() {
    let value = kani::any::<u8>();
    assert!(
        value.saturating_add(1) >= value,
        "saturation must not decrease a value"
    );
    kani::cover!(
        false,
        "this deliberately impossible witness must be rejected"
    );
}

/// Symbolic full-width multiplication deliberately exceeds the small solver budget.
#[kani::proof]
fn multiplication_timeout() {
    let left = kani::any::<u128>();
    let right = kani::any::<u128>();
    kani::assume(
        left > 1 && right > 1 && left <= u128::from(u64::MAX) && right <= u128::from(u64::MAX),
    );
    assert!(
        left.wrapping_mul(right) != 170141183460469231731687303715884105727,
        "the seeded factorization claim consumes the timeout budget"
    );
}

/// A deliberately insufficient bound exposes the unwinding assertion.
#[kani::proof]
#[kani::unwind(2)]
fn insufficient_unwind() {
    let count = kani::any::<u8>();
    kani::assume(count <= 4);
    let mut index = 0;
    while index < count {
        index += 1;
    }
    assert!(index == count, "the loop must reach its requested count");
}

/// Contradictory assumptions cannot justify a proof with a required witness.
#[kani::proof]
fn vacuous_assumption() {
    kani::assume(false);
    kani::cover!(true, "the contradictory assumption makes this unreachable");
}

/// Inline assembly is intentionally outside the supported verifier model.
#[kani::proof]
fn unsupported() {
    // SAFETY: an empty assembly block has no operands or observable effect.
    unsafe {
        core::arch::asm!("");
    }
}

/// Expected panics have different output semantics from ordinary successful claims.
#[kani::proof]
#[kani::should_panic]
fn expected_panic() {
    panic!("the corpus explicitly expects this panic");
}

/// A separately named mutant checks that saturation cannot wrap around.
#[kani::proof]
fn seeded_wrapping_mutant() {
    let value = kani::any::<u8>();
    assert!(
        value.wrapping_add(1) >= value,
        "the seeded wrapping mutant must fail at 255"
    );
}
