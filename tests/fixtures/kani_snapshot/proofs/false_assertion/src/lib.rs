// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Negative proof fixture: a harness that must fail verification.

/// Assert a symbolic condition that is false for one reachable input.
///
/// The harness must compile and run, then report a verification failure. A
/// transcript validator that accepted only "the process failed" could not tell
/// this apart from a compile error or a solver crash, so the smoke checks the
/// harness name and the verdict.
#[kani::proof]
fn deliberately_false_assertion() {
    let holds: bool = kani::any();
    kani::assert(holds, "deliberately false");
}