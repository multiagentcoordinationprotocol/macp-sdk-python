"""Tests for ``_decode_extensions`` (issue #93 item 2).

Before this fix, a bootstrap ``extensions`` value that was neither ``str``
nor ``bytes`` (e.g. an int, a list, a nested dict) was silently dropped from
the decoded map. It now raises, since the bootstrap schema declares every
value as a base64-encoded string.
"""

from __future__ import annotations

import base64

import pytest

from macp_sdk.agent.runner import _decode_extensions


class TestDecodeExtensions:
    def test_non_dict_input_returns_empty(self):
        assert _decode_extensions(None) == {}
        assert _decode_extensions([1, 2, 3]) == {}

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
