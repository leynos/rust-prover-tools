// Copyright Kani Contributors
// SPDX-License-Identifier: Apache-2.0 OR MIT

//! Upstream `tests/cargo-kani/build-rs-works`, unchanged.

#[kani::proof]
fn check() {
    assert!(env!("SET_IN_BUILD_RS") == "Y");
}