"""site_fetch: a Reddit thread and GitHub pages read from the sites' JSON
(ADR-0162). No network: `web_fetch.fetch` is replaced by a table of recorded
answers, shaped as the sites answer (GitHub's checked live on 2026-11-12;
Reddit's 403'd this laptop, so its shape is the documented listing pair)."""
from __future__ import annotations

import base64
import json
import re

import httpx
import pytest

from silica.sources import site_fetch as sf
from silica.sources import web_fetch as wf


def _resp(payload=None, text="", status=200, ctype="application/json"):
    content = json.dumps(payload).encode() if payload is not None else text.encode()
    return httpx.Response(status, headers={"content-type": ctype}, content=content,
                          request=httpx.Request("GET", "https://x.test/"))


@pytest.fixture
def served(monkeypatch):
    """{url prefix: response or exception}; every fetch is recorded with its headers."""
    table: dict = {}
    seen: list[tuple[str, dict]] = []

    def fake(url, headers=wf.HEADERS, **kw):
        seen.append((url, dict(headers)))
        for prefix, out in table.items():
            if url.startswith(prefix):
                if isinstance(out, Exception):
                    raise out
                return out, url
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(wf, "fetch", fake)
    return table, seen


# --- which addresses are the sites' ----------------------------------------------

@pytest.mark.parametrize("url", [
    "https://i.redd.it/abc.png", "https://www.reddit.com/r/python/", "https://www.reddit.com/user/x",
    "https://gist.github.com/x/abc", "https://docs.github.com/en/rest", "https://github.com/psf",
    "https://github.com/psf/requests/tree/main/docs", "https://example.com/r/x/comments/abc/",
    "https://reddit.com.evil.test/r/x/comments/abc/",
])
def test_other_pages_of_the_sites_take_the_html_path(served, url):
    assert sf.page(url) is None
    assert served[1] == []


# --- Reddit ---------------------------------------------------------------------

def _comment(cid, author, body, replies=None, score=3):
    return {"kind": "t1", "data": {"id": cid, "author": author, "body": body, "score": score,
                                   "created_utc": 1762900000,
                                   "replies": {"kind": "Listing", "data": {"children": replies}} if replies else ""}}


def _thread(comments, num_comments=None, **post):
    post = {"title": "Why  does this work?", "subreddit_name_prefixed": "r/learnpython", "author": "op",
            "created_utc": 1762800000, "score": 41, "is_self": True, "selftext": "Body &amp; *markdown*.",
            "permalink": "/r/learnpython/comments/abc123/why_does/", "url": "https://www.reddit.com/r/x",
            "num_comments": num_comments if num_comments is not None else len(comments), **post}
    return [{"kind": "Listing", "data": {"children": [{"kind": "t3", "data": post}]}},
            {"kind": "Listing", "data": {"children": comments}}]


@pytest.mark.parametrize("url,api", [
    ("https://www.reddit.com/r/learnpython/comments/abc123/why_does/", "/r/learnpython/comments/abc123/why_does.json"),
    ("https://old.reddit.com/r/learnpython/comments/abc123/why_does/def456/", "/r/learnpython/comments/abc123/why_does/def456.json"),
    ("https://new.reddit.com/r/learnpython/comments/abc123", "/r/learnpython/comments/abc123.json"),
    ("https://redd.it/abc123", "/comments/abc123.json"),
])
def test_a_permalink_or_short_link_reads_the_threads_json(served, url, api):
    table, seen = served
    table["https://www.reddit.com/"] = _resp(_thread([]))
    sf.page(url)
    asked, headers = seen[0]
    assert asked.startswith("https://www.reddit.com" + api + "?")
    assert "raw_json=1" in asked and "sort=top" in asked and "depth=3" in asked and "limit=200" in asked
    # a reader named Silica, and nothing else: no browser string, no person
    assert headers["User-Agent"].startswith("silica-agent/") and "Mozilla" not in headers["User-Agent"]


def test_the_note_keeps_the_post_and_the_comments_in_thread_order(served):
    table, _ = served
    deep = _comment("d4", "four", "too deep")
    tree = [_comment("c1", "alice", "First, top.", [
                _comment("c2", "bob", "A reply.", [_comment("c3", "carol", "Third level.", [deep])])]),
            _comment("c5", "dave", "Second top."),
            {"kind": "more", "data": {"count": 7, "children": ["x"]}}]
    table["https://www.reddit.com/"] = _resp(_thread(tree, num_comments=12, is_self=False,
                                                     url="https://example.com/paper"))

    page = sf.page("https://www.reddit.com/r/learnpython/comments/abc123/why_does/")

    assert page.title == "Why does this work?"
    text = page.text
    assert text.startswith("Source: https://www.reddit.com/r/learnpython/comments/abc123/why_does/\n\n")
    assert "r/learnpython · u/op · 2025-11-10 · 41 points" in text
    assert "Links to: https://example.com/paper" in text
    assert "Body &amp; *markdown*." in text  # verbatim, as raw_json gives it
    order = [text.index(s) for s in ("**u/alice**", "**u/bob** replying to u/alice",
                                     "**u/carol** replying to u/bob", "**u/dave**")]
    assert order == sorted(order)
    assert "too deep" not in text  # depth 3 at most
    assert "4 of 12 comments, top first and 3 levels deep; 8 left out." in text
    assert text.index("left out") < text.index("**u/alice**")  # said before the comments


def test_at_most_two_hundred_comments(served):
    table, _ = served
    table["https://www.reddit.com/"] = _resp(_thread([_comment(f"c{i}", f"u{i}", "ok") for i in range(260)]))
    text = sf.page("https://redd.it/abc123").text
    assert text.count("**u/u") == sf.REDDIT_COMMENTS
    assert "200 of 260 comments" in text and "60 left out" in text


def test_a_long_thread_stops_at_the_ceiling_between_comments_and_counts_the_rest(served):
    table, _ = served
    long = "word " * 400  # 2,000 characters a comment
    table["https://www.reddit.com/"] = _resp(_thread([_comment(f"c{i}", f"u{i}", long + str(i)) for i in range(40)]))
    text = sf.page("https://redd.it/abc123").text
    shown = text.count("**u/u")
    assert 0 < shown < 40 and len(text) <= wf._MAX_CHARS + 200
    assert f"{shown} of 40 comments, top first and 3 levels deep; {40 - shown} left out." in text
    assert text.rstrip().endswith(str(shown - 1))  # the last one kept is whole


@pytest.mark.parametrize("status,words", [(403, "Reddit refused to show this thread (403)"),
                                          (429, "Reddit is limiting reads from this address (429)")])
def test_reddits_refusal_is_named_and_nothing_is_written(served, status, words, tmp_vault, monkeypatch):
    table, _ = served
    table["https://www.reddit.com/"] = wf.Refused(status, "raw text", httpx.Headers())
    with pytest.raises(wf.SiteRefused, match=re.escape(words)):
        sf.page("https://www.reddit.com/r/x/comments/abc123/")

    from pathlib import Path

    from silica.config import CONFIG
    from silica.sources import web_research

    vault = Path(CONFIG.vault_path)
    before = sorted(vault.rglob("*"))
    with pytest.raises(wf.SiteRefused):
        web_research.fetch_to_inbox("https://www.reddit.com/r/x/comments/abc123/")
    assert sorted(vault.rglob("*")) == before


def test_one_comment_is_counted_as_one(served):
    table, _ = served
    table["https://www.reddit.com/"] = _resp(_thread([_comment("c1", "alice", "Only me.")]))
    text = sf.page("https://redd.it/abc123").text
    assert "1 comment, top first and 3 levels deep." in text and "All 1 comments" not in text


def test_a_code_fence_left_open_closes_where_its_post_or_comment_ends(served):
    """Reddit closes a fence at the end of the comment that opened it; the note
    did not, and every comment after it, with the note's Sources, read as code
    (driven in the GUI drawer, 2026-11-12)."""
    table, _ = served
    table["https://www.reddit.com/"] = _resp(_thread(
        [_comment("c1", "coder", "Try:\n\n```python\nprint(1)\n"), _comment("c2", "next", "Thanks.")],
        selftext="```\nopen post"))
    text = sf.page("https://redd.it/abc123").text
    assert text.index("open post") < text.index("\n```", text.index("open post")) < text.index("## Comments")
    assert text.index("print(1)") < text.index("\n```", text.index("print(1)")) < text.index("**u/next**")


def test_a_page_that_is_only_its_title_writes_nothing(served, tmp_vault):
    """A subreddit, to a reader with no Reddit session, is a script's shell whose
    only words are its title: /fetch wrote a note that said "Reddit" and nothing
    else (2026-11-12)."""
    from pathlib import Path

    from silica.config import CONFIG
    from silica.sources import web_research

    table, _ = served
    table["https://www.reddit.com/r/Python/"] = _resp(
        text="<html><head><title>Reddit</title></head><body><script>boot()</script></body></html>",
        ctype="text/html")
    vault = Path(CONFIG.vault_path)
    before = sorted(vault.rglob("*"))
    with pytest.raises(ValueError, match="nothing readable"):
        web_research.fetch_to_inbox("https://www.reddit.com/r/Python/")
    assert sorted(vault.rglob("*")) == before


def test_a_login_page_instead_of_the_listing_is_refused(served):
    table, _ = served
    table["https://www.reddit.com/"] = _resp(text="<html>log in</html>", ctype="text/html")
    with pytest.raises(wf.SiteRefused, match="did not answer with the thread"):
        sf.page("https://www.reddit.com/r/x/comments/abc123/")


# --- GitHub ---------------------------------------------------------------------

def _readme(text, name="README.md"):
    return _resp({"name": name, "encoding": "base64", "content": base64.b64encode(text.encode()).decode()})


def test_a_repository_gives_its_description_topics_and_readme(served):
    table, seen = served
    table["https://api.github.com/repos/psf/requests/readme"] = _readme("# Requests\n\n```py\nimport requests\n```\n")
    table["https://api.github.com/repos/psf/requests"] = _resp(
        {"full_name": "psf/requests", "description": "A simple, yet elegant, HTTP library.",
         "topics": ["http", "python"], "html_url": "https://github.com/psf/requests"})

    page = sf.page("https://github.com/psf/requests")

    assert page.title == "psf/requests"
    assert "A simple, yet elegant, HTTP library.\nTopics: http, python\nhttps://github.com/psf/requests" in page.text
    assert "## README.md\n\n# Requests\n\n```py\nimport requests\n```" in page.text  # Markdown as itself
    assert all(h["Accept"] == "application/vnd.github+json" for _, h in seen)


def test_a_readme_that_is_not_markdown_is_fenced(served):
    table, _ = served
    table["https://api.github.com/repos/o/r/readme"] = _readme("Plain\n````\ninside\n", name="README.rst")
    table["https://api.github.com/repos/o/r"] = _resp({"full_name": "o/r"})
    text = sf.page("https://github.com/o/r/").text
    # longer than the file's own run of four, so no line inside can close it
    assert "`````rst\nPlain\n````\ninside\n`````" in text


def test_a_blob_is_the_raw_file_fenced_when_it_is_not_markdown(served):
    table, seen = served
    table["https://raw.githubusercontent.com/"] = _resp(text="def get(url):\n    return 1\n", ctype="text/plain")
    page = sf.page("https://github.com/psf/requests/blob/main/src/requests/api.py#L10")
    assert seen[0][0] == "https://raw.githubusercontent.com/psf/requests/main/src/requests/api.py"
    assert page.title == "api.py (psf/requests)"
    assert "psf/requests: main/src/requests/api.py\n\n```py\ndef get(url):\n    return 1\n```" in page.text


def test_a_long_blob_is_cut_inside_its_fence_and_the_fence_closes(served):
    table, _ = served
    table["https://raw.githubusercontent.com/"] = _resp(text="x = 1\n" * 10_000, ctype="text/plain")
    text = sf.page("https://github.com/o/r/blob/main/big.py").text
    body, marker = text.rsplit("\n\n", 1)
    assert marker.startswith("[truncated at ") and body.endswith("\n```")


def test_an_issue_gives_its_state_author_body_and_every_comment_in_order(served):
    table, seen = served
    base = "https://api.github.com/repos/psf/requests/issues/6963"
    table[base + "/comments"] = _resp([{"user": {"login": "a"}, "created_at": "2025-06-04T11:00:00Z", "body": "one"},
                                       {"user": {"login": "b"}, "created_at": "2025-06-05T11:00:00Z", "body": "two"}])
    table[base] = _resp({"title": "fix CVE", "state": "closed", "state_reason": None, "comments": 2,
                         "created_at": "2025-06-04T10:59:08Z", "user": {"login": "awoimbee"}, "body": "The fix.",
                         "html_url": "https://github.com/psf/requests/pull/6963",
                         "pull_request": {"merged_at": "2025-06-05T14:55:33Z"}})

    page = sf.page("https://github.com/psf/requests/pull/6963/files")

    assert page.title == "fix CVE"
    text = page.text
    assert "Pull request #6963 in psf/requests · merged · opened by @awoimbee on 2025-06-04" in text
    assert "The fix." in text and "All 2 comments." in text
    assert text.index("**@a** · 2025-06-04\none") < text.index("**@b** · 2025-06-05\ntwo")
    assert not any("pulls" in url or ".diff" in url for url, _ in seen)  # the conversation, never the diff


def test_an_issue_closed_as_not_planned_says_so(served):
    table, _ = served
    table["https://api.github.com/"] = _resp({"state": "closed", "state_reason": "not_planned", "comments": 0,
                                              "user": {"login": "x"}})
    assert "Issue #1 in o/r · closed as not planned ·" in sf.page("https://github.com/o/r/issues/1").text


def test_githubs_rate_limit_is_named(served):
    table, _ = served
    table["https://api.github.com/"] = wf.Refused(403, "raw", httpx.Headers({"x-ratelimit-remaining": "0"}))
    with pytest.raises(wf.SiteRefused, match="60 an hour.*GITHUB_TOKEN"):
        sf.page("https://github.com/o/r/issues/1")


def test_a_missing_issue_is_named_and_a_missing_repo_falls_back_to_the_page(served):
    table, _ = served
    resp = _resp({"message": "Not Found"}, status=404)
    table["https://api.github.com/"] = httpx.HTTPStatusError("404", request=resp.request, response=resp)
    with pytest.raises(wf.SiteRefused, match="no issue or pull request there"):
        sf.page("https://github.com/o/r/issues/1")
    assert sf.page("https://github.com/features/copilot") is None


def test_the_token_goes_only_with_the_persons_own_fetch_on_a_laptop(served, monkeypatch, tmp_vault):
    """The person's /fetch carries it; the model's web_fetch, Discover and the
    check reach the same adapter on addresses a page named, and never do."""
    from silica.sources import web_research

    table, seen = served
    table["https://api.github.com/"] = _resp({"title": "t", "comments": 0, "state": "open"})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    web_research.fetch_to_inbox("https://github.com/o/r/issues/1")
    assert seen[-1][1]["Authorization"] == "Bearer ghp_secret"
    wf.web_fetch("https://github.com/o/r/issues/2")
    assert "Authorization" not in seen[-1][1]
    monkeypatch.setenv("SILICA_HOSTED", "1")
    web_research.fetch_to_inbox("https://github.com/o/r/issues/3")
    assert "Authorization" not in seen[-1][1]


def test_on_the_server_githubs_refusals_name_no_token(served, monkeypatch):
    table, _ = served
    monkeypatch.setenv("SILICA_HOSTED", "1")
    table["https://api.github.com/"] = wf.Refused(429, "raw", httpx.Headers())
    with pytest.raises(wf.SiteRefused) as got:
        sf.page("https://github.com/o/r/issues/1")
    assert "GITHUB_TOKEN" not in got.value.said and "try again later" in got.value.said


def test_a_redirect_to_another_host_drops_the_token(monkeypatch):
    """`fetch` follows redirects by hand with the same headers; a token is for
    the host it was sent to."""
    sent: list[tuple[str, dict]] = []

    class Hop:
        def __init__(self, status, location=None):
            self.status_code = status
            self.headers = httpx.Headers({"content-type": "application/json", **({"location": location} if location else {})})
            self.is_redirect = location is not None
            self.request = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield b"{}"

        def raise_for_status(self):
            pass

    hops = [Hop(302, "https://objects.example.net/blob"), Hop(200)]
    monkeypatch.setattr(wf, "_validated", lambda url: "93.184.216.34")
    monkeypatch.setattr(wf, "_stream", lambda url, ip, headers: (sent.append((url, headers)), hops.pop(0))[1])
    wf.fetch("https://api.github.com/x", {"Authorization": "Bearer t", "User-Agent": "u"})
    assert sent[0][1]["Authorization"] == "Bearer t"
    assert "Authorization" not in sent[1][1] and sent[1][1]["User-Agent"] == "u"


def test_the_vault_plus_shows_a_sites_refusal_in_its_own_words(tmp_vault, monkeypatch):
    from fastapi.testclient import TestClient

    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()

    def refuse(url):
        raise wf.SiteRefused("Reddit refused to show this thread (403): it turns away readers.")

    monkeypatch.setattr(wf, "fetch_page", refuse)
    row, = TestClient(server.app).post("/vault/fetch", json={"url": "https://redd.it/abc"}).json()["files"]
    assert row == {"name": "https://redd.it/abc", "status": "refused",
                   "reason": "Reddit refused to show this thread (403): it turns away readers."}
