# Kani snapshot smoke: bounded collection and cleanup

This fixture crate holds the named positive harnesses the consumer smoke
requires. `bounded_push_contents_are_exact` grows a small `Vec` under a fixed
bound and proves its contents. `raii_release_runs_once` proves that an owner
with a `Drop` implementation runs its cleanup exactly once at scope exit. Both
are deliberately small so the proof finishes quickly under CBMC.

The crate declares no dependency on `kani`: `cargo kani` supplies the proof
crate to the compiled fixture, exactly as it does for an ordinary consumer
crate. Nothing here is compiled by `make test`.
