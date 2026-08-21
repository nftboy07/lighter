"""
test_tempmail.py — Unit tests for TempMailProvider utilities.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from autobot.tempmail import TempMailProvider


class TestExtractLinks:
    def _extract(self, body):
        return TempMailProvider.extract_links(body)

    def test_plain_http(self):
        body = "Click here: http://example.com/verify?token=abc123"
        links = self._extract(body)
        assert "http://example.com/verify?token=abc123" in links

    def test_plain_https(self):
        body = "Verify at https://site.com/confirm/TOKEN123"
        links = self._extract(body)
        assert "https://site.com/confirm/TOKEN123" in links

    def test_verify_ranked_first(self):
        body = (
            "Unsubscribe: https://site.com/unsub\n"
            "Verify: https://site.com/verify?t=abc"
        )
        links = self._extract(body)
        assert links[0] == "https://site.com/verify?t=abc"

    def test_confirm_ranked_high(self):
        links = self._extract(
            "Random: https://x.com/home  Confirm: https://x.com/confirm/tk"
        )
        confirm = [l for l in links if "confirm" in l]
        assert confirm, "confirm link not found"
        assert links.index(confirm[0]) < links.index("https://x.com/home")

    def test_trailing_punctuation_stripped(self):
        links = self._extract("See <https://site.com/verify?t=x>.")
        assert any("verify" in l for l in links)
        for l in links:
            assert not l.endswith(">")

    def test_empty_body(self):
        assert self._extract("") == []
        assert self._extract(None) == []

    def test_deduplication(self):
        url = "https://site.com/verify?t=abc"
        body = f"{url} {url} {url}"
        links = self._extract(body)
        assert links.count(url) == 1
