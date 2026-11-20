Feature: Audit Rust sources for trusted items
  A trust audit reports where a proof asked its verifier to stop checking. It
  follows the module declarations a crate compiles, and refuses source it could
  not read, so a report never covers code the audit never saw.

  Scenario: Report a Kani assumption in a declared module
    Given a crate whose child module assumes a condition
    When the user audits the crate
    Then the command succeeds
    And stdout names the trusted item "src/child.rs:2:11: assume"

  Scenario: Report a proof that asks for no trust
    Given a crate whose child module assumes nothing
    When the user audits the crate
    Then the command succeeds
    And stdout reports "audited 2 files, 0 trusted items"

  Scenario: Report the audit as JSON
    Given a crate whose child module assumes a condition
    When the user audits the crate as JSON
    Then the command succeeds
    And stdout reports the JSON item "src/child.rs" at line 2 column 11

  Scenario: Refuse a declaration that names no file
    Given a crate declaring a module that does not exist
    When the user audits the crate
    Then the command fails
    And stderr names the module "absent"

  Scenario: Refuse an entry point outside the repository
    Given a source file outside the repository root
    When the user audits the crate
    Then the command fails
    And stderr reports the file as outside the repository root