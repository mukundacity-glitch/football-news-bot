from pathlib import Path

path = Path("tools/_apply_fotmob_contract_patch.py")
text = path.read_text(encoding="utf-8")

old_fixture = '''    rt = VerificationRuntime.create(db_path=tmp_path / "verification.sqlite3", fpl_data=fpl)\n    yield rt\n    rt.close()\n'''
new_fixture = '''    rt = VerificationRuntime(\n        fpl_data=fpl, database_path=tmp_path / "verification.sqlite3"\n    )\n    yield rt\n    rt.close()\n'''
if text.count(old_fixture) != 1:
    raise RuntimeError(f"expected generated runtime fixture once, found {text.count(old_fixture)}")
text = text.replace(old_fixture, new_fixture, 1)

anchor = '''replace_once(\n    "tests/test_autopost_unlimited.py",\n    \'\'\'    assert main._live_event_allowed({"event": "renewal"}) is False\'\'\',\n    \'\'\'    assert main._live_event_allowed({"event": "renewal"}) is True\'\'\',\n)\n\n# 6) Regression tests based on the structured shape shown in FotMob Transfer Center.\n'''
if text.count(anchor) != 1:
    raise RuntimeError(f"expected test-update anchor once, found {text.count(anchor)}")

prefix = '''replace_once(\n    "tests/test_autopost_unlimited.py",\n    \'\'\'    assert main._live_event_allowed({"event": "renewal"}) is False\'\'\',\n    \'\'\'    assert main._live_event_allowed({"event": "renewal"}) is True\'\'\',\n)\n\n'''
legacy_update = '''replace_once(\n    "tests/test_verification_v2.py",\n    \'\'\'def test_official_contract_extension_is_rejected_out_of_scope(runtime):\n    # Strict policy: only TRANSFER / INJURY / SUSPENSION may publish. Even an\n    # official club confirmation of a contract extension must be rejected.\n    obs = observation(\n        title="Brighton confirm Danny Welbeck has signed a new contract",\n        source_id="club.brighton-and-hove-albion",\n        url="https://www.brightonandhovealbion.com/pages/en/media-article/welbeck-new-contract",\n        story={\n            "player": "Danny Welbeck", "event": "renewal", "to_key": "Brighton",\n            "to_club": "Brighton", "stage": 4,\n        },\n    )\n    decision = runtime.verify_observations([obs])\n    assert decision.decision == DecisionType.REJECT, decision.reasons\n    assert not decision.may_publish\n\'\'\',\n    \'\'\'def test_official_contract_extension_without_term_fails_closed(runtime):\n    # CONTRACT is now a supported category, but a vague extension announcement\n    # still cannot publish without an explicitly grounded contract end/term.\n    obs = observation(\n        title="Brighton confirm Danny Welbeck has signed a new contract",\n        source_id="club.brighton-and-hove-albion",\n        url="https://www.brightonandhovealbion.com/pages/en/media-article/welbeck-new-contract",\n        story={\n            "player": "Danny Welbeck", "event": "renewal", "to_key": "Brighton",\n            "to_club": "Brighton", "stage": 4,\n        },\n    )\n    decision = runtime.verify_observations([obs])\n    assert decision.decision == DecisionType.PENDING, decision.reasons\n    assert not decision.may_publish\n    mandatory = decision.gate("mandatory_facts")\n    assert not mandatory.passed\n    assert "contract_length" in mandatory.reason\n\'\'\',\n)\n\n'''
comment = '# 6) Regression tests based on the structured shape shown in FotMob Transfer Center.\n'
text = text.replace(anchor, prefix + legacy_update + comment, 1)
path.write_text(text, encoding="utf-8")
print("Corrected FotMob contract tests and legacy expectation")
