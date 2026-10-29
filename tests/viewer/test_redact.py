from tools.viewer.viewer_app.redact import redact, safe_text


def test_credentials_are_masked_and_html_is_escaped() -> None:
    text = "sk-viewertest0000 Bearer viewer-test-value api_key=viewer-test-value <script>x</script>"
    assert redact(text) == "sk-*** Bearer *** api_key=[已打码] <script>x</script>"
    escaped = safe_text(text)
    assert "sk-***" in escaped and "Bearer ***" in escaped and "api_key=[已打码]" in escaped
    assert "&lt;script&gt;" in escaped and "<script>" not in escaped
