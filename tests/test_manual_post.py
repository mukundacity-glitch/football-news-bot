from tools.manual_post import _target_key


def test_manual_story_id_normalizes_to_v2_key():
    assert _target_key("abc123") == "v2_abc123"
    assert _target_key("v2_abc123") == "v2_abc123"


def test_manual_story_id_requires_value():
    try:
        _target_key("   ")
    except ValueError as exc:
        assert "required" in str(exc)
    else:
        raise AssertionError("empty manual story id must fail closed")
