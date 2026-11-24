// Copyright 2026 leynos
// SPDX-License-Identifier: ISC

//! Positive proof fixtures for bounded growth and ownership-driven cleanup.

use std::cell::Cell;

/// Grow a vector to exactly three values under a fixed capacity bound.
fn bounded_push() -> Vec<u32> {
    let mut values = Vec::with_capacity(3);
    for value in 1..=3 {
        values.push(value);
    }
    values
}

/// An owner whose drop increments the shared release counter.
///
/// The counter is a `Cell` so the harness can observe it while the owner
/// still holds the borrow that its `Drop` implementation uses.
struct ReleaseCounter<'a> {
    released: &'a Cell<u32>,
}

impl Drop for ReleaseCounter<'_> {
    fn drop(&mut self) {
        self.released.set(self.released.get() + 1);
    }
}

#[kani::proof]
#[kani::unwind(4)]
fn bounded_push_contents_are_exact() {
    // Three pushes plus the terminating branch: unwind 4 admits the loop.
    let values = bounded_push();
    kani::assert(values.len() == 3, "three bounded pushes");
    kani::assert(values[0] == 1, "first value");
    kani::assert(values[1] == 2, "second value");
    kani::assert(values[2] == 3, "third value");
}

#[kani::proof]
fn raii_release_runs_once() {
    let released = Cell::new(0u32);
    {
        let _counter = ReleaseCounter {
            released: &released,
        };
        kani::assert(released.get() == 0, "the owner has not dropped yet");
    }
    kani::assert(released.get() == 1, "cleanup ran exactly once");
}