Feature: Analyse captured Kani logs
  A captured log is decided against an explicit harness manifest, so a
  successful printed verdict is never mistaken for a passing claim.

  Scenario: Analyse a verified log
    Given the captured Kani log "verified"
    And the corpus harness manifest
    And the selected harness "harnesses::verified"
    When the user analyses the Kani log
    Then the command succeeds
    And stdout reports outcome "verified"
    And stdout reports cover "the boundary below saturation is reachable" with status "satisfied"

  Scenario: Report a counterexample
    Given the captured Kani log "counterexample"
    And the corpus harness manifest
    And the selected harness "harnesses::counterexample"
    When the user analyses the Kani log
    Then the command completes without a passing analysis
    And stdout reports outcome "counterexample"

  Scenario: Refuse a truncated log as a tool error
    Given the captured Kani log "truncated"
    And the corpus harness manifest
    When the user analyses the Kani log
    Then the command completes without a passing analysis
    And stdout reports outcome "tool-error"
    And stdout reports the structural reason

  Scenario: Reject an invalid manifest
    Given the captured Kani log "verified"
    And an invalid harness manifest
    When the user analyses the Kani log
    Then the command fails
    And stderr names the invalid manifest

  Scenario: Emit machine-readable analysis
    Given the captured Kani log "default_unwind"
    And the corpus harness manifest
    And the selected harness "harnesses::verified"
    And the selected harness "harnesses::seeded_wrapping_mutant"
    When the user analyses the Kani log in JSON
    Then the command succeeds
    And stdout is a stable JSON analysis with outcome "verified" and run mode "full-profile"