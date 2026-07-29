"""web_fetch: URL guards, HTML extraction, the fetch loop, the YouTube branch.

No real network (httpx.get and socket.getaddrinfo are monkeypatched) and no
real subprocess (subprocess.run is monkeypatched).
"""
from __future__ import annotations

import pytest

from silica.sources import web_fetch as wf


# --- host_matches -----------------------------------------------------------

def test_host_matches_exact_and_subdomain():
    assert wf.host_matches("https://youtube.com/watch?v=a", "youtube.com")
    assert wf.host_matches("https://www.youtube.com/watch?v=a", "youtube.com")
    assert wf.host_matches("https://m.youtube.com/watch?v=a", "youtube.com")
    assert wf.host_matches("https://youtu.be/a", "youtube.com", "youtu.be")


def test_host_matches_is_case_and_trailing_dot_insensitive():
    assert wf.host_matches("https://YouTube.COM./watch", "youtube.com")


def test_host_matches_rejects_suffix_lookalike():
    # substring matching would pass this one
    assert not wf.host_matches("https://youtube.com.evil.test/a", "youtube.com")


def test_host_matches_rejects_userinfo_disguise():
    # urlsplit reads the real host as evil.test
    assert not wf.host_matches("https://youtube.com@evil.test/", "youtube.com")


def test_host_matches_rejects_non_http_scheme():
    assert not wf.host_matches("file:///etc/passwd", "etc")
    assert not wf.host_matches("ftp://youtube.com/a", "youtube.com")


def test_host_matches_rejects_malformed_port():
    assert not wf.host_matches("https://youtube.com:notaport/a", "youtube.com")


def test_host_matches_no_domains_is_false():
    assert not wf.host_matches("https://youtube.com/a")


# --- _validated -------------------------------------------------------------

def _resolves_to(monkeypatch, *ips: str):
    """Pin getaddrinfo so no test ever hits DNS."""
    def fake(host, port, *a, **kw):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(wf.socket, "getaddrinfo", fake)


def test_validated_accepts_a_global_address(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    wf._validated("https://example.com/a")  # must not raise


def test_validated_rejects_loopback(monkeypatch):
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(ValueError, match="non-global"):
        wf._validated("https://localhost.evil.test/")


def test_validated_rejects_cloud_metadata_endpoint(monkeypatch):
    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(ValueError, match="non-global"):
        wf._validated("https://metadata.evil.test/")


def test_validated_rejects_rfc1918(monkeypatch):
    _resolves_to(monkeypatch, "10.0.0.5")
    with pytest.raises(ValueError, match="non-global"):
        wf._validated("https://intranet.evil.test/")


def test_validated_rejects_when_any_resolved_address_is_private(monkeypatch):
    # one global answer must not launder a private one
    _resolves_to(monkeypatch, "93.184.216.34", "192.168.1.1")
    with pytest.raises(ValueError, match="non-global"):
        wf._validated("https://split.evil.test/")


def test_validated_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="non-HTTP"):
        wf._validated("file:///etc/passwd")


def test_validated_rejects_embedded_credentials():
    with pytest.raises(ValueError, match="credentials"):
        wf._validated("https://user:pw@example.com/")


def test_validated_rejects_hostless_url():
    with pytest.raises(ValueError, match="no host"):
        wf._validated("http:///path")


def test_validated_rejects_garbage():
    # no scheme at all, so the scheme check fires before the host check
    with pytest.raises(ValueError, match="non-HTTP"):
        wf._validated("not a url")


def test_validated_reports_dns_failure(monkeypatch):
    def boom(*a, **kw):
        raise wf.socket.gaierror("nope")
    monkeypatch.setattr(wf.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="cannot resolve"):
        wf._validated("https://nx.evil.test/")


# --- extraction -------------------------------------------------------------

_PAGE = """<html><head><title>Real Title</title>
<style>body{color:red}</style></head>
<body>
  <nav>Home Login Signup</nav>
  <header>Site banner</header>
  <article><p>First paragraph.</p><p>Second   paragraph &amp; more.</p></article>
  <form><input name="q"></form>
  <footer>Copyright notice</footer>
  <script>var tracking = 1;</script>
</body></html>"""


def test_extract_keeps_prose_and_title():
    out = wf._extract_text(_PAGE)
    assert "Real Title" in out
    assert "First paragraph." in out
    assert "Second paragraph & more." in out  # entities decoded, runs collapsed


def test_extract_drops_boilerplate_tags():
    out = wf._extract_text(_PAGE)
    for noise in ("color:red", "Home Login", "Site banner", "Copyright notice",
                  "var tracking"):
        assert noise not in out


def test_extract_separates_block_elements():
    # open and close both emit a break, so blocks land one blank line apart
    assert wf._extract_text("<p>one</p><p>two</p>") == "one\n\ntwo"


def test_extract_collapses_blank_runs():
    assert wf._extract_text("<p>a</p><div></div><div></div><div></div><p>b</p>") == "a\n\nb"


def test_extract_survives_unbalanced_tags():
    # a stray close tag must not drive the skip counter negative and swallow
    # the rest of the page
    out = wf._extract_text("</script><p>visible</p>")
    assert "visible" in out


def test_extract_of_empty_html_is_empty():
    assert wf._extract_text("") == ""


# --- truncation -------------------------------------------------------------

def test_truncate_is_a_noop_under_the_limit():
    assert wf._truncate("short", limit=100) == "short"


def test_truncate_marks_the_cut():
    out = wf._truncate("x" * 500, limit=100)
    assert out.startswith("x" * 100)
    assert "truncated at 100 characters" in out
