Feature: Prover tools command-line interface
  The CLI replaces repository shell scripts with deterministic Kani and Verus
  workflows.

  Scenario: Check the installed Kani version
    Given a repository with Kani version "0.61.0"
    And the Kani command reports version "0.61.0"
    When the user checks the Kani version
    Then the command succeeds
    And stdout contains the matching Kani version message

  Scenario: Reject a mismatched Kani version
    Given a repository with Kani version "0.61.0"
    And the Kani command reports version "0.60.0"
    When the user checks the Kani version
    Then the command fails
    And stderr contains the Kani version mismatch message

  Scenario: Run a Verus proof successfully
    Given a repository with Verus version "0.1.0"
    And the Verus toolchain is installed
    When the user runs a Verus proof on "example.rs"
    Then the command succeeds
    And stdout contains the proof verification result

  Scenario: Reject a Verus proof with missing toolchain
    Given a repository with Verus version "0.1.0"
    And the Verus toolchain is not installed
    When the user runs a Verus proof on "example.rs"
    Then the command fails
    And stderr contains the missing toolchain message

  Scenario: Verify several Verus proof files in one invocation
    Given a repository with Verus version "0.1.0"
    And the Verus proof for "passing.rs" is accepted
    And the Verus proof for "failing.rs" is rejected with exit code 3
    And a Verus binary that reports per-file proof verdicts
    When the user runs Verus proofs on "passing.rs" and "failing.rs"
    Then the command fails
    And stdout contains the proof verification result for "passing.rs"
    And stderr names the failing proof file "failing.rs"
    And stderr reports the proof file proved before the failure "passing.rs"
    And the command exits with the prover exit code 3
