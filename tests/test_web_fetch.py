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


# --- the fetch loop ---------------------------------------------------------

import httpx
from types import SimpleNamespace

from silica.tools import TOOLS


class _Resp:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status=200, text="", ctype="text/html", location=None):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": ctype} if ctype else {}
        self.is_redirect = location is not None
        self.next_request = SimpleNamespace(url=location) if location else None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None
            )


def _serve(monkeypatch, *responses, allow_all_dns=True):
    """Queue responses for successive httpx.get calls; record requested URLs."""
    seen: list[str] = []
    queue = list(responses)

    def fake_get(url, **kw):
        seen.append(url)
        assert kw.get("follow_redirects") is False, "redirects must be manual"
        return queue.pop(0)

    monkeypatch.setattr(wf.httpx, "get", fake_get)
    if allow_all_dns:
        monkeypatch.setattr(
            wf.socket, "getaddrinfo",
            lambda host, port, *a, **kw: [(2, 1, 6, "", ("93.184.216.34", port))],
        )
    return seen


def test_web_fetch_registered_and_sensitive():
    assert "web_fetch" in TOOLS
    assert TOOLS["web_fetch"].sensitive is True


def test_web_fetch_returns_extracted_text_under_a_source_header(monkeypatch):
    _serve(monkeypatch, _Resp(text="<html><body><p>Hello world.</p></body></html>"))
    out = wf.web_fetch("https://example.com/a")
    assert out.splitlines()[0] == "Source: https://example.com/a"
    assert "Hello world." in out


def test_web_fetch_follows_redirects_and_reports_the_final_url(monkeypatch):
    seen = _serve(
        monkeypatch,
        _Resp(status=302, location="https://example.com/final"),
        _Resp(text="<p>arrived</p>"),
    )
    out = wf.web_fetch("https://example.com/start")
    assert seen == ["https://example.com/start", "https://example.com/final"]
    assert out.splitlines()[0] == "Source: https://example.com/final"


def test_web_fetch_revalidates_every_redirect_hop(monkeypatch):
    """A global first hop must not launder a redirect into link-local space."""
    seen: list[str] = []

    def fake_get(url, **kw):
        seen.append(url)
        return _Resp(status=302, location="http://169.254.169.254/latest/meta-data/")

    def fake_dns(host, port, *a, **kw):
        ip = "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr(wf.httpx, "get", fake_get)
    monkeypatch.setattr(wf.socket, "getaddrinfo", fake_dns)

    with pytest.raises(ValueError, match="non-global"):
        wf.web_fetch("https://example.com/redirector")
    assert seen == ["https://example.com/redirector"]  # second hop never issued


def test_web_fetch_caps_the_redirect_chain(monkeypatch):
    hop = _Resp(status=302, location="https://example.com/next")
    _serve(monkeypatch, *[hop] * (wf._MAX_REDIRECTS + 1))
    with pytest.raises(ValueError, match="redirects"):
        wf.web_fetch("https://example.com/loop")


def test_web_fetch_403_says_bot_wall(monkeypatch):
    _serve(monkeypatch, _Resp(status=403))
    with pytest.raises(ValueError, match="403"):
        wf.web_fetch("https://example.com/paywalled")


def test_web_fetch_429_says_rate_limited(monkeypatch):
    _serve(monkeypatch, _Resp(status=429))
    with pytest.raises(ValueError, match="rate limited"):
        wf.web_fetch("https://example.com/busy")


def test_web_fetch_500_still_raises(monkeypatch):
    _serve(monkeypatch, _Resp(status=500))
    with pytest.raises(httpx.HTTPStatusError):
        wf.web_fetch("https://example.com/broken")


def test_web_fetch_refuses_binary_content(monkeypatch):
    _serve(monkeypatch, _Resp(ctype="application/pdf", text="%PDF-1.7"))
    with pytest.raises(ValueError, match="application/pdf"):
        wf.web_fetch("https://example.com/paper.pdf")


def test_web_fetch_passes_plain_text_through_unparsed(monkeypatch):
    _serve(monkeypatch, _Resp(ctype="text/plain", text="a <b> c"))
    out = wf.web_fetch("https://example.com/robots.txt")
    assert "a <b> c" in out  # not run through the HTML parser


def test_web_fetch_truncates_long_pages(monkeypatch):
    _serve(monkeypatch, _Resp(text="<p>" + ("word " * 40_000) + "</p>"))
    out = wf.web_fetch("https://example.com/long")
    assert "[truncated at" in out
    assert len(out) < wf._MAX_CHARS + 200


def test_web_fetch_rejects_a_private_target_before_any_request(monkeypatch):
    called = {"n": 0}

    def fake_get(url, **kw):
        called["n"] += 1
        return _Resp()

    monkeypatch.setattr(wf.httpx, "get", fake_get)
    monkeypatch.setattr(
        wf.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [(2, 1, 6, "", ("127.0.0.1", port))],
    )
    with pytest.raises(ValueError, match="non-global"):
        wf.web_fetch("http://localhost:8080/admin")
    assert called["n"] == 0


# --- YouTube ----------------------------------------------------------------

from pathlib import Path

_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
hello <00:00:01.000><c>world</c>

00:00:02.000 --> 00:00:04.000
hello world

00:00:04.000 --> 00:00:06.000
second line &amp; more
"""


def test_vtt_to_text_strips_timings_markup_and_rolling_duplicates():
    assert wf._vtt_to_text(_VTT).splitlines() == [
        "hello world",
        "second line & more",
    ]


def test_youtube_without_ytdlp_prescribes_the_install(monkeypatch):
    monkeypatch.setattr(wf.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="yt-dlp"):
        wf.web_fetch("https://www.youtube.com/watch?v=abc")


def test_youtube_never_takes_the_http_path(monkeypatch):
    monkeypatch.setattr(wf.shutil, "which", lambda name: None)

    def boom(*a, **kw):
        raise AssertionError("httpx.get must not run for a YouTube URL")

    monkeypatch.setattr(wf.httpx, "get", boom)
    with pytest.raises(ValueError, match="yt-dlp"):
        wf.web_fetch("https://youtu.be/abc")


def _fake_ytdlp(monkeypatch, *, writes=True, stderr=""):
    monkeypatch.setattr(wf.shutil, "which", lambda name: "/usr/bin/yt-dlp")

    def fake_run(argv, **kw):
        if writes:
            out = Path(argv[argv.index("-o") + 1])
            out.with_suffix(".en.vtt").write_text(_VTT, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr=stderr)

    monkeypatch.setattr(wf.subprocess, "run", fake_run)


def test_youtube_returns_the_transcript(monkeypatch):
    _fake_ytdlp(monkeypatch)
    out = wf.web_fetch("https://www.youtube.com/watch?v=abc")
    assert out.splitlines()[0] == "Source: https://www.youtube.com/watch?v=abc"
    assert "second line & more" in out


def test_youtube_without_subtitles_reports_the_stderr_tail(monkeypatch):
    _fake_ytdlp(monkeypatch, writes=False, stderr="ERROR: no subtitles available")
    with pytest.raises(ValueError, match="no subtitles available"):
        wf.web_fetch("https://youtu.be/abc")


def test_youtube_lookalike_domain_takes_the_http_path(monkeypatch):
    _serve(monkeypatch, _Resp(text="<p>not youtube</p>"))
    out = wf.web_fetch("https://youtube.com.evil.test/watch?v=abc")
    assert "not youtube" in out


def test_youtube_userinfo_on_a_real_host_does_not_reach_ytdlp(monkeypatch):
    """`urlsplit` already resolves `youtube.com@evil.test` to host `evil.test`,
    so that disguise never needed the userinfo guard: it fails the domain
    check regardless. The guard earns its keep on the opposite shape, where
    `.hostname` genuinely IS youtube.com but userinfo is riding along
    (`x@youtube.com`). The YouTube branch shells out to yt-dlp with no
    `_validated()` call of its own, so `host_matches` is the only gate; without
    the guard this URL would route straight to `subprocess.run`."""
    monkeypatch.setattr(wf.shutil, "which", lambda name: "/usr/bin/yt-dlp")

    def boom(*a, **kw):
        raise AssertionError("subprocess.run must not run: host_matches should reject userinfo")

    monkeypatch.setattr(wf.subprocess, "run", boom)
    with pytest.raises(ValueError, match="credentials"):
        wf.web_fetch("https://x@youtube.com/watch?v=abc")
