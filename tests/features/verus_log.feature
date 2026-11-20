Feature: Analyse captured Verus logs
  A captured log is decided from the summary and diagnostics Verus printed, so
  a run that never reached a result is never mistaken for a passing proof.

  Scenario: Analyse a verified log
    Given the captured Verus log "verified"
    When the user analyses the Verus log
    Then the command succeeds
    And stdout reports the Verus outcome "verified"
    And stdout reports the run mode "full"

  Scenario: Report a postcondition failure
    Given the captured Verus log "postcondition"
    When the user analyses the Verus log
    Then the command completes without passing
    And stdout reports the Verus outcome "errors"
    And stdout names the failure "postcondition.rs:8:13"

  Scenario: Refuse a contradictory log as a tool error
    Given a log whose clean summary sits beside a failure
    When the user analyses the Verus log
    Then the command completes without passing
    And stdout reports the Verus outcome "tool-error"
    And stdout reports a structural reason

  Scenario: Reject an unreadable log
    Given the captured Verus log "absent"
    When the user analyses the Verus log
    Then the command fails
    And stderr names the unreadable log

  Scenario: Accept a negative control that fails where it was meant to
    Given the captured Verus log "postcondition"
    And the declared failure region "postcondition.rs:5-12"
    When the user analyses the Verus log
    Then the command succeeds
    And stdout reports the declared expectation as matched

  Scenario: Reject a negative control that fails elsewhere
    Given the captured Verus log "postcondition"
    And the declared failure region "postcondition.rs:20"
    When the user analyses the Verus log
    Then the command completes without passing
    And stdout reports the declared expectation as unmatched

  Scenario: Require a full run
    Given the captured Verus log "selected_function"
    And a full run is required
    When the user analyses the Verus log
    Then the command completes without passing
    And stdout reports the run mode "partial"

  Scenario: Emit machine-readable analysis
    Given the captured Verus log "two_errors"
    When the user analyses the Verus log in JSON
    Then the command completes without passing
    And stdout is a stable Verus JSON analysis with outcome "errors" and run mode "full"