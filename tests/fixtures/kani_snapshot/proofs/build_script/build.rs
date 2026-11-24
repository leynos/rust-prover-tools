// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Build script for the upstream `build-rs-works` bundle smoke.

fn main() {
    println!("cargo:rustc-cfg=smoke_build_script");
}