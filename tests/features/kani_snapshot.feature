Feature: Isolated Kani snapshot installation and execution
  The snapshot commands install one reviewed Kani build into a private cache,
  revalidate it before every proof and never fall back to a host toolchain.

  Scenario: Install a snapshot from verified local assets
    Given a repository with a reviewed Kani snapshot pin
    When the user installs the snapshot
    Then the command succeeds
    And stdout reports the installed snapshot

  Scenario: Check an installed snapshot
    Given a repository with a reviewed Kani snapshot pin
    And the snapshot is installed
    When the user checks the snapshot
    Then the command succeeds
    And stdout reports the checked snapshot

  Scenario: Run a proof through the installed snapshot
    Given a repository with a reviewed Kani snapshot pin
    And the snapshot is installed
    When the user runs the snapshot launcher with "--harness" "example"
    Then the snapshot launcher exit status is 7
    And stdout contains the forwarded argument "--harness"
    And stdout contains the forwarded argument "example"

  Scenario: Refuse changed assets without deleting the previous generation
    Given a repository with a reviewed Kani snapshot pin
    And the snapshot is installed
    And the active generation receipt is corrupt
    And the local snapshot assets change after installation
    When the user installs the snapshot
    Then the command fails
    And stderr reports the changed snapshot asset
    And the previous generation directory is preserved

  Scenario: Refuse the upstream setup command through the snapshot
    Given a repository with a reviewed Kani snapshot pin
    When the user runs the upstream setup command through the snapshot
    Then the command fails
    And stderr refuses the setup command

  Scenario: Refuse the Lean backend through the snapshot
    Given a repository with a reviewed Kani snapshot pin
    When the user selects the Lean backend through the snapshot
    Then the command fails
    And stderr refuses the Lean backend