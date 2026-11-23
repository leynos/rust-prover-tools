//! A file whose single assertion does not hold.
use vstd::prelude::*;

verus! {

fn bounded(value: u64) -> (result: u64)
    requires value < 10,
    ensures result < 10,
{
    let result = value + 5;
    assert(result < 10);
    result
}

fn main() {}

} // verus!
