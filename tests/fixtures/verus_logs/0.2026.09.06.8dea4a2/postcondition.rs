//! A file whose single function fails its postcondition.
use vstd::prelude::*;

verus! {

fn doubled(value: u64) -> (result: u64)
    requires value < 1000,
    ensures result == value + 1,
{
    value + 2
}

fn main() {}

} // verus!
