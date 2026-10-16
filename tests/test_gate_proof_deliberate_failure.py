"""Deliberately failing test for #739 proof that a failing test fails `gate`. Reverted before merge."""


def test_gate_proof_deliberate_failure():
    assert False, "deliberate failure: gate must go red (#739 proof)"
