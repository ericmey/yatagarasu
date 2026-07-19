def test_this_must_fail_to_prove_ci_blocks_merges():
    """Deliberate failure. If a PR carrying this can still merge, the required
    status check is not doing its job and we are back to trusting discipline."""
    assert False, "intentional failure — proving CI is a gate, not a suggestion"
