from fakeyou.foundation.digest import canonical_json_digest

PAYLOAD = {"b": [1, 2.5, "é"], "a": {"x": None, "y": True}}


def test_compact_ascii_sha256_matches_the_persona_and_brief_encoding() -> None:
    assert (
        canonical_json_digest(PAYLOAD, compact=True)
        == "6524f6bf1c082ac7687e243230290ff0c411778d671f11fc9405bdf7145b3973"
    )


def test_default_separators_keep_non_ascii_text_unescaped() -> None:
    assert (
        canonical_json_digest(PAYLOAD, ensure_ascii=False)
        == "793da7f29914ce251cf77337390563b0dd8a21e244a1cbd222825635de7c761c"
    )


def test_sha1_default_encoding_matches_the_calibration_fingerprint() -> None:
    assert (
        canonical_json_digest(PAYLOAD, algorithm="sha1")
        == "31093c03b7b410607bec3f218349d902dc8be275"
    )


def test_key_order_does_not_change_the_digest() -> None:
    assert canonical_json_digest({"a": 1, "b": 2}) == canonical_json_digest({"b": 2, "a": 1})


def test_ascii_escaping_changes_the_digest() -> None:
    assert canonical_json_digest("é", ensure_ascii=True) != canonical_json_digest(
        "é", ensure_ascii=False
    )


def test_compact_separators_change_the_digest() -> None:
    assert canonical_json_digest(PAYLOAD, compact=True) != canonical_json_digest(PAYLOAD)
