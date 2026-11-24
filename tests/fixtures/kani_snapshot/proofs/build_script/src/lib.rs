// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Library for the upstream `build-rs-works` bundle smoke.
//!
//! The upstream bundle test builds and verifies a crate that carries a build
//! script, so this fixture does the same: `build.rs` emits a cfg that the
//! harness requires.

/// Return the value the build script's cfg selects.
#[cfg(smoke_build_script)]
fn build_script_value() -> u32 {
    7
}

#[cfg(not(smoke_build_script))]
fn build_script_value() -> u32 {
    0
}

#[kani::proof]
fn build_script_cfg_is_set() {
    kani::assert(build_script_value() == 7, "the build script ran");
}