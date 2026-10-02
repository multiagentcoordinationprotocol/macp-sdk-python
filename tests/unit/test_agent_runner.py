"""Tests for ``_decode_extensions`` (issue #93 item 2).

Before this fix, a bootstrap ``extensions`` value that was neither ``str``
nor ``bytes`` (e.g. an int, a list, a nested dict) was silently dropped from
the decoded map. It now raises, since the bootstrap schema declares every
value as a base64-encoded string.
"""

from __future__ import annotations

import base64
import logging

import pytest

from macp_sdk.agent.runner import _decode_extensions


class TestDecodeExtensions:
    def test_absent_extensions_returns_empty(self):
        """None (the common no-extensions bootstrap, ss.get("extensions")
        with the key missing) is the one input that legitimately means
        'nothing to decode' -- not silently-dropped malformed input."""
        assert _decode_extensions(None) == {}

    def test_non_dict_present_value_raises(self):
        """A *present* non-object extensions value (#97 follow-up) is
        equally malformed as a bad per-value entry and must not be
        silently dropped either."""
        with pytest.raises(ValueError, match="must be an object"):
            _decode_extensions([1, 2, 3])
        with pytest.raises(ValueError, match="must be an object"):
            _decode_extensions("not a map")

    def test_bytes_value_passes_through(self):
        assert _decode_extensions({"k": b"raw"}) == {"k": b"raw"}

    def test_valid_base64_string_is_decoded(self):
        encoded = base64.b64encode(b"hello").decode("ascii")
        assert _decode_extensions({"k": encoded}) == {"k": b"hello"}

    def test_non_base64_string_falls_back_to_utf8_bytes(self):
        assert _decode_extensions({"k": "not base64!!"}) == {"k": b"not base64!!"}

    def test_non_str_non_bytes_value_raises(self):
        with pytest.raises(ValueError, match=r"extensions\['k'\]"):
            _decode_extensions({"k": 42})

    def test_non_str_non_bytes_value_raises_for_nested_containers_too(self):
        with pytest.raises(ValueError, match="got list"):
            _decode_extensions({"k": [1, 2]})
        with pytest.raises(ValueError, match="got dict"):
            _decode_extensions({"k": {"nested": True}})

    def test_non_base64_string_emits_one_debug_record_naming_the_key(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="macp_sdk"):
            result = _decode_extensions({"k": "not base64!!"})

        assert result == {"k": b"not base64!!"}
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug_records) == 1
        assert "k" in debug_records[0].getMessage()

    def test_valid_base64_string_emits_no_debug_record(self, caplog):
        encoded = base64.b64encode(b"hello").decode("ascii")
        with caplog.at_level(logging.DEBUG, logger="macp_sdk"):
            result = _decode_extensions({"k": encoded})

        assert result == {"k": b"hello"}
        assert len(caplog.records) == 0

    @pytest.mark.parametrize("ambiguous", ["abcd", "pack"])
    def test_base64_first_heuristic_is_the_decided_behavior(self, ambiguous, caplog):
        """Pins issue #121's decision: a plain string that is also valid
        base64 decodes as base64, not as its literal UTF-8 bytes -- a known,
        accepted ambiguity, not something to silently start treating
        differently. See _decode_extensions's docstring for the rationale."""
        with caplog.at_level(logging.DEBUG, logger="macp_sdk"):
            result = _decode_extensions({"k": ambiguous})

        assert result == {"k": base64.b64decode(ambiguous)}
        assert result != {"k": ambiguous.encode("utf-8")}
        assert len(caplog.records) == 0
