import io
import json
from unittest.mock import patch

import pytest

from tools import capture_state, prepare_recovery


def test_capture_is_one_get_and_never_exposes_api_key(monkeypatch):
    monkeypatch.setenv("SV08_MOONRAKER_API_KEY", "test-secret-not-real")
    calls = []

    def open_url(request, timeout):
        calls.append(request)
        assert request.get_method() == "GET"
        assert request.full_url.startswith("http://127.0.0.1/printer/objects/query?")
        assert request.get_header("X-api-key") == "test-secret-not-real"
        assert request.data is None
        return io.BytesIO(b'{"result":{"status":{"bed_mesh":{}}}}')

    with patch.object(capture_state, "urlopen", open_url):
        result = capture_state.capture("http://127.0.0.1")
    assert len(calls) == 1
    assert "test-secret-not-real" not in json.dumps(result)


def test_capture_rejects_nonhttp_and_url_credentials():
    for url in ("file:///etc/passwd", "http://user:pass@localhost"):
        with pytest.raises(ValueError):
            capture_state.capture(url)


def test_recipe_requires_review_and_does_not_rebind_another_source(bundle):
    data, source, _, _ = bundle
    with pytest.raises(ValueError, match="reviewed"):
        prepare_recovery.prepare(data, source)
    assert (
        prepare_recovery.prepare(data, source, reviewed=True)["source_sha256"]
        == data["source_sha256"]
    )
    data["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different source"):
        prepare_recovery.prepare(data, source, reviewed=True)
