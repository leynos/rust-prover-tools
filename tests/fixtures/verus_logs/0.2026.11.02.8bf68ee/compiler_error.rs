//! A file Verus cannot compile, so verification never starts.
use vstd::prelude::*;

verus! {

fn broken(value: u64) -> (result: u64)
    ensures result == value + 1,
{
    let text: u64 = "not a number";
    value + 1
}

fn main() {}

} // verus!
