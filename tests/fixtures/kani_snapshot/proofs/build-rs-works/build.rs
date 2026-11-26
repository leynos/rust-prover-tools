// Copyright Kani Contributors
// SPDX-License-Identifier: Apache-2.0 OR MIT

//! The `build-rs-works` fixture's build script.
//!
//! It emits `cargo:rustc-env=SET_IN_BUILD_RS=Y`, and the `check` proof in
//! `src/lib.rs` asserts that same value with `env!`, so the fixture proves the
//! bundle runs a build script whose environment reaches the compiler.

fn main() {
    println!("cargo:rustc-env=SET_IN_BUILD_RS=Y");
}