//! A file whose every function Verus verifies.
use vstd::prelude::*;

verus! {

fn increment_value(value: u64) -> (result: u64)
    requires value < u64::MAX,
    ensures result == value + 1,
{
    value + 1
}

fn decrement_value(value: u64) -> (result: u64)
    requires value > 0,
    ensures result == value - 1,
{
    value - 1
}

fn main() {}

} // verus!
