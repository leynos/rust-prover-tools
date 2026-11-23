//! A file whose two functions each fail their postcondition.
use vstd::prelude::*;

verus! {

fn first(value: u64) -> (result: u64)
    requires value < 1000,
    ensures result == value + 1,
{
    value + 2
}

fn second(value: u64) -> (result: u64)
    requires value < 1000,
    ensures result == value + 3,
{
    value + 4
}

fn main() {}

} // verus!
