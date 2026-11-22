// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Positive proof fixtures for bounded growth and ownership-driven cleanup.

/// Grow a vector to exactly three values under a fixed capacity bound.
fn bounded_push() -> Vec<u32> {
    let mut values = Vec::with_capacity(3);
    for value in 1..=3 {
        values.push(value);
    }
    values
}

/// An owner whose drop increments the caller's release counter.
struct ReleaseCounter<'a> {
    released: &'a mut u32,
}

impl Drop for ReleaseCounter<'_> {
    fn drop(&mut self) {
        *self.released += 1;
    }
}

#[kani::proof]
fn bounded_push_and_drop_are_exact() {
    let values = bounded_push();
    kani::assert(values.len() == 3, "three bounded pushes");
    kani::assert(values[0] == 1, "first value");
    kani::assert(values[1] == 2, "second value");
    kani::assert(values[2] == 3, "third value");
}

#[kani::proof]
fn raii_release_runs_once() {
    let mut released = 0u32;
    {
        let _counter = ReleaseCounter {
            released: &mut released,
        };
        kani::assert(released == 0, "the owner has not dropped yet");
    }
    kani::assert(released == 1, "cleanup ran exactly once");
}