//! A file whose call site fails the callee's precondition.
use vstd::prelude::*;

verus! {

fn doubled(value: u64) -> (result: u64)
    requires value < 1000,
    ensures result == value * 2,
{
    value * 2
}

fn caller(value: u64) -> (result: u64)
    ensures result == value * 2,
{
    doubled(value)
}

fn main() {}

} // verus!
