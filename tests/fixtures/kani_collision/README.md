# Kani 0.67.0 target-attribution collisions

These captures record what Kani 0.67.0 does when a pretty name appears in two
Cargo targets, or when a target's `required-features` are unmet. They are the
measurement behind the attribution rules in
[the supervised execution reference](../../../docs/kani-supervised-execution.md);
no gate parses them.

All three come from one scratch package `collision` with a library proof
`collision_proof` and an integration-test copy of the same name, run on this
host with `~/.kani/kani-0.67.0`.

- `required-features.txt`: the test target declared
  `required-features = ["gated"]`.
  `cargo kani --tests --harness collision_proof --exact` aborted the build and
  emitted no record.
- `cfg-disabled-test-copy.txt`: the same package without `required-features`,
  with the test copy behind `#[cfg(feature = "gated")]`. The library copy was
  the only record and it passed.
- `duplicate-pretty-name.txt`: both copies compiled. Kani printed two records
  for the one name and reported one success and one failure.
