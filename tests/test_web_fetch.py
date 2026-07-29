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
