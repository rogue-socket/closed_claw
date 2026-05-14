# Purpose: Unit tests for CoordinatorNodes._verify_acceptance_criteria.

from __future__ import annotations

from closed_claw.coordinator.nodes import CoordinatorNodes


def test_no_criteria_passes():
    """A subtask with no acceptance_criteria is accepted regardless of result."""
    item = {"acceptance_criteria": []}
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(item, "anything")
    assert ok is True
    assert reason == ""


def test_absolute_path_criterion_rejects_unresolved_home_var():
    """A result containing the literal $HOME doesn't satisfy an 'absolute path' criterion."""
    item = {"acceptance_criteria": ["The absolute path to the folder is retrieved."]}
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(
        item, "The absolute path is $HOME/Desktop"
    )
    assert ok is False
    assert reason == "unresolved_env_var_in_absolute_path"


def test_absolute_path_criterion_accepts_resolved_path():
    """A result with a real /-rooted path passes the 'absolute path' criterion."""
    item = {"acceptance_criteria": ["The absolute path is retrieved."]}
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(
        item, "The absolute path is /Users/yashagrawal/Desktop"
    )
    assert ok is True
    assert reason == ""


def test_absolute_path_criterion_rejects_tilde():
    """A result with a tilde-shorthand path doesn't satisfy 'absolute path'."""
    item = {"acceptance_criteria": ["The absolute path is retrieved."]}
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(
        item, "The path is ~/Desktop"
    )
    assert ok is False
    assert reason == "unresolved_env_var_in_absolute_path"


def test_criterion_match_is_case_insensitive():
    """'Absolute Path' / 'ABSOLUTE path' still triggers the check."""
    item = {"acceptance_criteria": ["The Absolute Path must be returned."]}
    ok, _ = CoordinatorNodes._verify_acceptance_criteria(item, "$HOME/x")
    assert ok is False


def test_absolute_path_criterion_rejects_result_without_any_path():
    """A bare 'done' string doesn't satisfy an 'absolute path' criterion."""
    item = {"acceptance_criteria": ["The absolute path must be returned."]}
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(item, "done")
    assert ok is False
    assert reason == "no_absolute_path_in_result"


# ── self-reported failure phrases (safety net for the agent's status field) ──


def test_self_reported_failure_phrase_is_rejected():
    """The verbatim phrasing from sim 2 should fail acceptance."""
    item = {"acceptance_criteria": []}
    text = (
        "The task to fetch a webpage and count the number of <p> tags "
        "could not be completed. The web_fetch tool strips HTML tags."
    )
    ok, reason = CoordinatorNodes._verify_acceptance_criteria(item, text)
    assert ok is False
    assert reason == "agent_self_reported_failure_in_result_text"


def test_other_failure_phrasings_are_rejected():
    """Several common 'I gave up' phrasings should fail acceptance."""
    item = {"acceptance_criteria": []}
    for text in [
        "I was unable to complete the task because the tool is missing.",
        "Cannot complete the task without network access.",
        "The task cannot be completed with the available tools.",
    ]:
        ok, reason = CoordinatorNodes._verify_acceptance_criteria(item, text)
        assert ok is False, text
        assert reason == "agent_self_reported_failure_in_result_text"


def test_could_not_in_legitimate_success_is_accepted():
    """Legitimate uses of 'could not' (not tied to the task itself) must pass.

    Anchor the failure phrases to 'the task / this task' so that valid
    results like 'could not find any matching rows, so the count is 0'
    aren't flagged.
    """
    item = {"acceptance_criteria": []}
    ok, _ = CoordinatorNodes._verify_acceptance_criteria(
        item, "I could not find any matching rows, so the count is 0."
    )
    assert ok is True
    ok, _ = CoordinatorNodes._verify_acceptance_criteria(
        item, "The query returned 5 rows; one user could not be matched."
    )
    assert ok is True
