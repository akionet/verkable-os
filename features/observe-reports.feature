Feature: Observe automation reports through a tenant-scoped read-only API
  Background:
    Given real automation reports are configured for an authorized reader

  @OBS-001-B1-01
  Scenario: Reports come from the index in newest-first order
    When the reader requests the report index
    Then the four reports are returned newest first

  @OBS-001-B1-02
  Scenario: Invalid report content is an error rather than data
    When a report fails schema validation
    Then Observe returns a typed invalid-report error

  @OBS-001-B1-03
  Scenario: Unauthorized readers cannot discover an organization
    When a reader without organization access requests every Observe path
    Then each organization path is indistinguishable from an unknown organization

  @OBS-001-B1-04
  Scenario: The current pending-human queue comes from the checkpoint
    When the reader requests the current pending-human queue
    Then the queue contains the checkpoint gates with age and issue links

  @OBS-001-B1-05
  Scenario: Observe exposes no write route
    When the reader inspects the Observe routes
    Then every Observe route is GET or HEAD only
