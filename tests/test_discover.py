"""Discover (ADR-0137): the feed reader, the cover rules, the story's citation
rule, saving, the store and the routes. No network: every fetch is faked."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from silica.sources import web_fetch as wf
from silica.sources.discover import feeds as F
from silica.sources.discover import rank, read, store
from silica.sources.discover import story as S

# --- the DNS pin (ADR-0137 point 11, web_fetch.py's TOCTOU closure) ---------------


class _Resp:
    request = None

    def __init__(self, status=200, body=b"", ctype="text/html", headers=None):
        self.status_code = status
        self.headers = httpx.Headers({"content-type": ctype, **(headers or {})})
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=None)


def _dns(monkeypatch, ip="93.184.216.34"):
    monkeypatch.setattr(wf.socket, "getaddrinfo", lambda host, port, *a, **kw: [(2, 1, 6, "", (ip, port))])


def test_each_hop_connects_to_the_address_its_check_approved(monkeypatch):
    seen = []
    queue = [_Resp(302, headers={"location": "/next"}), _Resp(body=b"<p>ok</p>")]

    def fake(url, ip, headers):
        seen.append((url, ip))
        return queue.pop(0)

    _dns(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(wf, "_stream", fake)
    resp, final = wf.fetch("https://example.com/start")
    # The relative Location resolves against the URL as named, never the IP.
    assert seen == [("https://example.com/start", "93.184.216.34"), ("https://example.com/next", "93.184.216.34")]
    assert final == "https://example.com/next"


def test_the_stream_sends_the_name_as_host_and_sni(monkeypatch):
    sent = {}

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def build_request(self, method, url, headers, extensions):
            sent.update(url=url, headers=headers, ext=extensions)
            return SimpleNamespace()

        def send(self, req, stream):
            return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(wf.httpx, "Client", Client)
    with wf._stream("https://example.com:8443/a?b=1", "2001:db8::1", {"User-Agent": "x"}):
        pass
    assert sent["url"] == "https://[2001:db8::1]:8443/a?b=1"
    assert sent["headers"]["Host"] == "example.com:8443"
    assert sent["ext"] == {"sni_hostname": "example.com"}


def test_a_304_comes_back_to_the_caller_that_asked_for_it(monkeypatch):
    _dns(monkeypatch)
    monkeypatch.setattr(wf, "_stream", lambda url, ip, headers: _Resp(304, ctype=""))
    resp, _ = wf.fetch("https://example.com/feed", {"If-None-Match": "x"}, types=wf.FEED_TYPES)
    assert resp.status_code == 304 and resp.content == b""


def test_a_cover_is_taken_only_on_its_word(monkeypatch):
    _dns(monkeypatch)
    monkeypatch.setattr(wf, "_stream", lambda url, ip, headers: _Resp(body=b"x", ctype=""))
    with pytest.raises(ValueError, match="untyped"):
        wf.fetch("https://example.com/i", types=("image/",))
    monkeypatch.setattr(wf, "_stream", lambda url, ip, headers: _Resp(body=b"x" * 50, ctype="image/jpeg"))
    with pytest.raises(ValueError, match="passed 10 bytes"):
        wf.fetch("https://example.com/i", types=("image/",), cap=10)


def test_og_image_is_read_with_a_parser_not_a_pattern():
    # One of the three measured pages writes content before property, unquoted.
    page = '<html><head><meta content=https://cdn.test/c.jpg property=og:image></head></html>'
    assert wf.page_image(page, "https://site.test/a") == "https://cdn.test/c.jpg"
    assert wf.page_image('<meta name="twitter:image" content="/t.png">', "https://site.test/a") == "https://site.test/t.png"
    assert wf.page_image("<p>none</p>", "https://site.test/a") == ""


# --- feeds -----------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Desk</title>
<item><title>Thumb before content</title><link>https://n.test/1</link>
  <media:thumbnail url="https://img.test/thumb.jpg"/>
  <media:content url="https://img.test/content.jpg" medium="image"/>
  <pubDate>Tue, 27 Oct 2026 20:00:00 GMT</pubDate><dc:creator>A, B</dc:creator></item>
<item><title>Untyped content with an image name</title><link>https://n.test/2</link>
  <media:content url="https://img.test/photo.jpeg?w=1"/></item>
<item><title>Video content is no cover</title><link>https://n.test/3</link>
  <media:content url="https://img.test/clip.mp4" type="video/mp4"/>
  <enclosure url="https://img.test/enc.png" type="image/png"/></item>
<item><title>Body image</title><link>https://n.test/4</link>
  <description><![CDATA[<p>Lead text.</p><img src='https://img.test/body.webp'>]]></description></item>
<item><title>No image</title><link>https://n.test/5</link><description>Just words.</description></item>
</channel></rss>"""


def test_the_cover_order_is_the_items_own_and_empty_means_none():
    title, arts = F.parse(RSS, "https://n.test/feed")
    assert title == "Desk"
    images = [a["image"] for a in arts]
    assert images == ["https://img.test/content.jpg", "https://img.test/photo.jpeg?w=1",
                      "https://img.test/enc.png", "https://img.test/body.webp", ""]
    assert arts[0]["authors"] == ["A", "B"] and arts[0]["published"]
    assert arts[3]["lead"] == "Lead text."


def test_a_description_that_is_only_a_link_is_no_summary():
    """Hacker News sends <a href="...item?id=...">Comments</a> as every item's description;
    a summary that says only "Comments" is no summary, and a description with words keeps them."""
    hn = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Hacker News</title>
<item><title>Limits merging</title><link>https://o.test/a</link>
  <description><![CDATA[<a href="https://news.ycombinator.com/item?id=45">Comments</a>]]></description></item>
<item><title>Words and a link</title><link>https://o.test/b</link>
  <description><![CDATA[Real summary. <a href="https://x.test/c">Comments</a>]]></description></item>
</channel></rss>"""
    _, arts = F.parse(hn, "https://news.ycombinator.com/rss")
    assert [a["lead"] for a in arts] == ["", "Real summary. Comments"]


def test_atom_and_json_feed():
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
      <entry><title>E</title><link rel="alternate" href="/e1"/><updated>2026-10-27T10:00:00Z</updated>
      <summary>S</summary></entry></feed>"""
    t, arts = F.parse(atom, "https://a.test/feed")
    assert t == "A" and arts[0]["url"] == "https://a.test/e1" and arts[0]["published"]
    jf = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "J",
                     "items": [{"url": "https://j.test/1", "title": "T", "content_html": "<p>x</p><img src='/i.png'>"}]})
    t, arts = F.parse(jf.encode(), "https://j.test/feed.json")
    assert t == "J" and arts[0]["image"] == "https://j.test/i.png"
    with pytest.raises(ValueError):
        F.parse(b"<html><body>no</body></html>", "https://x.test")


def test_a_logo_shared_by_three_items_is_not_a_cover():
    arts = [{"image": "https://x.test/logo.png"} for _ in range(F.LOGO_REPEATS)] + [{"image": "https://x.test/a.jpg"}] * 2
    F.drop_logos(arts)
    assert [a["image"] for a in arts] == ["", "", "", "https://x.test/a.jpg", "https://x.test/a.jpg"]


def test_what_a_typed_address_is():
    assert F.classify("cs.CC") == ("arxiv", "https://rss.arxiv.org/rss/cs.CC")
    assert F.classify("youtube.com/@3blue1brown")[0] == "youtube"
    assert F.classify("it.wikipedia.org/wiki/X") == ("wikipedia", "https://it.wikipedia.org")
    assert F.classify("example.org") == ("url", "https://example.org")


def test_feed_discovery_and_opml():
    html = '<head><link rel="alternate" type="application/rss+xml" href="/rss"></head>'
    found = F.discover_links(html, "https://s.test/blog/post")
    assert found[0] == "https://s.test/rss" and "https://s.test/feed" in found
    opml = b"""<opml version="2.0"><body><outline text="Tech"><outline text="Ars" xmlUrl="https://a.test/f"/></outline>
      <outline text="Top" xmlUrl="https://t.test/f"/></body></opml>"""
    assert F.parse_opml(opml) == [{"url": "https://a.test/f", "name": "Ars", "tags": ["Tech"]},
                                  {"url": "https://t.test/f", "name": "Top", "tags": []}]


def test_the_catalog_trim_keeps_name_kind_language_and_feeds():
    from importlib import import_module
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    trim = import_module("discover_catalog").trim
    rows = trim({"AI": {"category_type": "topic", "source_language": "en", "display_names": {"it": "IA"},
                        "feeds": ["https://a.test/f", "ftp://no"]},
                 "Rome": {"category_type": "city", "feeds": ["https://r.test/f"]}, "Empty": {"feeds": []}})
    assert rows == [{"name": "AI", "kind": "topic", "lang": "en", "feeds": ["https://a.test/f"]},
                    {"name": "Rome", "kind": "city", "lang": "", "feeds": ["https://r.test/f"]}]


def test_the_shipped_catalog_reads():
    from importlib import resources

    doc = json.loads(resources.files("silica.sources.discover").joinpath("catalog.json").read_text(encoding="utf-8"))
    assert sum(1 for c in doc["categories"] if c["kind"] == "topic") == 192
    assert resources.files("silica.sources.discover").joinpath("LICENSE-kite-feeds").read_text().startswith("MIT License")


# --- the story's citation rule (ADR-0137 point 5) ------------------------------------


def test_a_sentence_with_no_marker_is_dropped_and_a_late_marker_kept():
    body = ("Bolsonaro left for the U.S. after the vote [1]. He was seen in Miami. [2] Mr. Lula said nothing.\n\n"
            "## Who checks\nThe review is led by its authors [2, 3]. Nobody knows when.\n\n"
            "## Empty\nNothing here is cited.")
    out, dropped = S.drop_unmarked(body)
    assert out == ("Bolsonaro left for the U.S. after the vote [1]. He was seen in Miami. [2]\n\n"
                   "## Who checks\nThe review is led by its authors [2, 3].")
    assert dropped == 3  # Lula, Nobody knows when, Nothing here is cited
    secs = S.sections(out)
    assert [s["title"] for s in secs] == ["", "Who checks"]
    assert secs[1]["paras"][0]["cites"] == [2, 3] and secs[1]["articles"] == 2


def test_a_quotation_is_one_sentence_and_an_ap_month_ends_none():
    # G2, 2026-10-31: a quotation was cut in three with only its cited tail kept,
    # and "Nov. 4" was cut in two.
    quote = "“We needed to get there. It was not heavier. It was not thicker. It came with the screen,” he said [1]."
    body = quote + " The summit opens Nov. 4 at 9 a.m. [2] Nobody explained."
    out, dropped = S.drop_unmarked(body)
    assert out == quote + " The summit opens Nov. 4 at 9 a.m. [2]"
    assert dropped == 1   # "Nobody explained."


def test_only_quotes_the_article_contains_are_banked(monkeypatch):
    reply = json.dumps({"quotes": [
        {"article": 1, "quote": "The vote was  close.", "why": "result"},
        {"article": 1, "quote": "A sentence the page never said.", "why": "invented"},
        {"article": 2, "quote": "Turnout was 77%.", "why": "turnout"},
        {"article": 9, "quote": "Turnout was 77%.", "why": "no such article"}],
        "terms": ["Knesset"], "questions": ["Who won?", "What next?", "Why?"]})
    monkeypatch.setattr("silica.model.llm.call_llm", lambda *a, **k: SimpleNamespace(text="```json\n" + reply + "\n```"))
    arts = [{"url": "https://a.test/1"}, {"url": "https://b.test/2"}]
    bank, terms, qs = S.bank_quotes("H", arts, ["Intro. The vote was close. End.", "Turnout was 77%."])
    assert [(k, q.url) for k, q in bank.items()] == [("Q1", "https://a.test/1"), ("Q2", "https://b.test/2")]
    assert terms == ["Knesset"] and qs == ["Who won?", "What next?", "Why?"]


# --- the store, saving and the chat block --------------------------------------------


def _doc_with_story(sid="s1", written=None, updated=None):
    doc = store.load()
    doc["setup"] = True
    doc["articles"]["a1"] = {"id": "a1", "url": "https://n.test/1", "title": "Headline", "lead": "The lead.",
                             "domain": "n.test", "bucket": "news", "tags": ["World"]}
    doc["stories"][sid] = {"id": sid, "articles": ["a1"], "bucket": "news", "tags": ["World"],
                           "created": time.time(), "updated": updated or time.time(), "n": 1,
                           "cover": None, "written": written, "near": None}
    store.save(doc)
    return doc


def test_prune_keeps_saved_stories_and_drops_the_rest_past_the_window(tmp_vault):
    old = time.time() - (store.KEEP_DAYS + 1) * 86400
    _doc_with_story("s1", updated=old)
    doc = store.load()
    doc["stories"]["s2"] = dict(doc["stories"]["s1"], id="s2")
    doc["saved"]["s2"] = "Inbox/x.md"
    vectors = {"s1": [1.0], "s2": [1.0]}
    assert store.prune(doc, vectors, now=time.time()) == 1
    assert set(doc["stories"]) == {"s2"} and set(vectors) == {"s2"} and "a1" in doc["articles"]


def test_saving_an_unopened_story_writes_its_articles_and_undo_removes_it(tmp_vault):
    from pathlib import Path

    from silica.config import CONFIG

    _doc_with_story()
    path = S.save(None, "s1")
    note = (Path(CONFIG.vault_path) / path).read_text(encoding="utf-8")
    assert "source: discover" in note and "> The lead." in note and "https://n.test/1" in note
    assert store.load()["saved"]["s1"] == path
    S.unsave(None, "s1", path)
    assert not (Path(CONFIG.vault_path) / path).exists() and not store.load()["saved"]


def test_the_chat_block_fences_the_articles_and_names_the_scope(tmp_vault):
    _doc_with_story()
    block, chip = S.chat_block(None, "s1", "vault")
    assert block.startswith("<discover-story>") and block.endswith("</discover-story>")
    assert "<article-data>" in block and S.SCOPES["vault"] in block and "[1] Headline - https://n.test/1" in block
    assert chip == {"id": "s1", "title": "Headline", "articles": 1}
    from silica.agent.mentions import strip

    assert strip("my question\n\n" + block) == "my question"


def test_less_like_this_leaves_for_you():
    doc = {"stories": {}, "feedback": {"b": -1}, "saved": {}, "topics": {}}
    stories = [{"id": i, "articles": ["x"], "updated": time.time(), "tags": [], "near": None} for i in "ab"]
    assert [s["id"] for s in rank.for_you(doc, stories)] == ["a"]


def test_a_near_floor_belongs_to_the_embedder_it_was_measured_on():
    from silica.config import EMBEDDER_DEFAULTS

    # pplx scores the same stories about 0.7 times what jina does, so jina's 0.40
    # would show a hosted tenant almost nothing; an id nobody measured keeps 0.40.
    assert rank.near_at("perplexity/pplx-embed-v1-4b") == 0.28
    assert rank.near_at("jina-embeddings-v5-text-small-retrieval") == rank.NEAR_AT == 0.40
    assert rank.near_at("perplexity/pplx-embed-v1-0.6b") == rank.NEAR_AT   # never measured, not the 4b
    assert rank.near_at("") == rank.NEAR_AT
    assert all(0 < float(row.get("near_at", rank.NEAR_AT)) < 1 for row in EMBEDDER_DEFAULTS.values())


def test_near_notes_keep_what_clears_the_configured_embedders_floor(tmp_vault, monkeypatch):
    import numpy as np

    from silica.config import CONFIG
    from silica.kernel.recall import embed

    class Store:
        def __len__(self):
            return 2

        def cosine_top_k(self, vec, k=5):
            return [{"path": "A/near", "name": "near", "score": 0.33}, {"path": "B/far", "name": "far", "score": 0.20}]

    monkeypatch.setattr(embed, "get_store", lambda: Store())
    for model, expect in (("perplexity/pplx-embed-v1-4b", [{"path": "A/near.md", "name": "near", "score": 0.33}]),
                          ("jina-embeddings-v5-text-small-retrieval", [])):
        _doc_with_story()
        store.save_vectors({"s1": np.array([1.0, 0.0], dtype=np.float32)})
        monkeypatch.setattr(CONFIG, "embedding_model", model)
        rank.near_notes(None, ["s1"])
        assert store.load()["stories"]["s1"]["near"] == expect, model


def test_tags_in_their_three_yaml_spellings():
    assert rank.note_tags("tags: [a, b]") == ["a", "b"]
    assert rank.note_tags("tags: a, #b") == ["a", "b"]
    assert rank.note_tags("tags:\n  - concepts\n  - 'biology'\nnext: 1") == ["concepts", "biology"]


def test_an_old_item_is_not_news(tmp_vault):
    doc = store.load()
    feed = {"id": "f", "bucket": "news", "name": "F", "tags": []}
    new = read._merge(doc, {"f": [{"url": "https://n.test/old", "title": "o", "lead": "", "image": "",
                                   "published": time.time() - 400 * 86400},
                                  {"url": "https://n.test/new", "title": "n", "lead": "", "image": "",
                                   "published": None}]}, {"f": feed})
    assert [doc["articles"][a]["url"] for a in new] == ["https://n.test/new"]


def test_a_read_mends_the_summary_its_own_feed_serves_again(tmp_vault):
    doc = store.load()
    feed = {"id": "f", "bucket": "news", "name": "F", "tags": []}
    item = {"url": "https://n.test/a", "title": "a", "lead": "Comments", "image": "", "published": None}
    read._merge(doc, {"f": [item]}, {"f": feed})
    assert read._merge(doc, {"f": [{**item, "lead": ""}]}, {"f": feed}) == []
    assert doc["articles"][F.article_id(item["url"])]["lead"] == ""
    # another feed carrying the same address leaves the first feed's summary alone
    read._merge(doc, {"g": [{**item, "lead": "Other words"}]}, {"g": {**feed, "id": "g"}})
    assert doc["articles"][F.article_id(item["url"])]["lead"] == ""


def test_grouping_joins_an_article_to_the_nearest_story(tmp_vault, monkeypatch):
    import numpy as np

    doc = store.load()
    for aid, title in (("a", "x"), ("b", "y"), ("c", "z")):
        doc["articles"][aid] = {"id": aid, "title": title, "lead": "", "bucket": "news", "tags": []}
    vecs = {"x": [1.0, 0.0], "y": [0.99, 0.141], "z": [0.0, 1.0]}
    monkeypatch.setattr(read, "_embed", lambda texts: np.array(
        [np.array(vecs[t.split(".")[0]]) / np.linalg.norm(vecs[t.split(".")[0]]) for t in texts], dtype=np.float32))
    vectors: dict = {}
    read.group(doc, vectors, ["a", "b", "c"])
    groups = sorted(sorted(s["articles"]) for s in doc["stories"].values())
    assert groups == [["a", "b"], ["c"]]


def test_a_changed_embedder_leaves_old_stories_as_they_are_and_groups_on(tmp_vault, monkeypatch):
    import numpy as np

    doc = store.load()
    doc["articles"]["a"] = {"id": "a", "title": "x", "lead": "", "bucket": "news", "tags": []}
    doc["stories"]["old"] = {"id": "old", "articles": ["z"], "bucket": "news", "tags": [], "created": 0, "updated": 0,
                             "n": 1, "cover": None, "written": None, "near": None}
    vectors = {"old": np.ones(4, dtype=np.float32) / 2}   # the embedder before: 4 wide
    monkeypatch.setattr(read, "_embed", lambda texts: np.tile(np.array([1.0, 0.0, 0.0], dtype=np.float32), (len(texts), 1)))
    read.group(doc, vectors, ["a"])   # 3 wide now: used to raise in `mat @ v` on every read
    assert set(doc["stories"]) == {"old", "sa"} and doc["stories"]["old"]["articles"] == ["z"]
    assert set(vectors) == {"sa"}
    store.save_vectors(vectors)   # one width, so the file can be written


# --- the routes ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_vault, monkeypatch):
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    monkeypatch.setattr(read, "start", lambda vault=None, force=False: False)
    return TestClient(server.app)


def test_first_run_then_feeds_catalog_and_opml(client, monkeypatch):
    d = client.get("/discover").json()
    assert d["setup"] is False and set(d["tabs"]) == {"foryou", "news", "research", "video"}
    s = client.get("/discover/setup").json()
    assert s["counts"] == {"news": 12, "research": 2, "reference": 2, "video": 3}
    assert client.post("/discover/setup", json={"buckets": {"video": False}, "topics": []}).json() == {"ok": True}
    assert client.get("/discover").json()["setup"] is True
    r = client.post("/discover/feeds", json={"url": "math.CO", "bucket": "news", "tags": "Combinatorics"})
    assert r.status_code == 200 and r.json()["feed"]["kind"] == "arXiv category"
    assert client.post("/discover/feeds", json={"url": "math.CO"}).status_code == 409
    assert client.post("/discover/catalog/add", json={"name": "AI"}).json()["feeds"] == 91
    labels = [b["label"] for b in client.get("/discover/feeds").json()["buckets"]]
    assert "AI" in labels
    opml = b'<opml><body><outline text="X" xmlUrl="https://x.test/feed"/></body></opml>'
    r = client.post("/discover/opml", files={"file": ("f.opml", opml, "text/xml")})
    assert r.json() == {"added": 1, "listed": 1}


def test_a_feedless_site_is_refused_on_the_hosted_pilot(client, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")  # a seat: Free has no Discover (ADR-0140)

    def no_feed(url, state=None, **kw):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>no feed</p>"), url

    monkeypatch.setattr(read, "_get", no_feed)
    r = client.post("/discover/feeds", json={"url": "nofeed.test"})
    assert r.status_code == 422 and "reads feeds only" in r.json()["detail"]


def test_a_cover_name_is_checked_before_a_file_is_opened(client):
    assert client.get("/discover/cover/..%2F..%2Fdiscover.json").status_code == 404
    assert client.get("/discover/cover/" + "a" * 24 + ".jpg").status_code == 404


def test_a_site_icon_is_fetched_once_and_only_for_a_site_in_the_store(client, monkeypatch):
    _doc_with_story()
    _dns(monkeypatch)
    calls, png = [], b"\x89PNG\r\n\x1a\n" + b"\0" * 20

    def stream(url, ip, headers):
        calls.append(url)
        return _Resp(body=png, ctype="image/x-icon")  # nytimes.com's shape: a PNG typed as an icon

    monkeypatch.setattr(wf, "_stream", stream)
    r = client.get("/discover/icon/n.test")
    assert r.status_code == 200 and r.content == png and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    assert client.get("/discover/icon/n.test").status_code == 200
    assert client.get("/discover/icon/other.test").status_code == 404
    assert client.get("/discover/icon/..%2Fdiscover.json").status_code == 404
    assert calls == ["https://n.test/favicon.ico"]


def test_an_icon_that_is_no_raster_or_too_big_is_refused_and_asked_again_only_after_days(client, monkeypatch):
    import os

    _doc_with_story()
    _dns(monkeypatch)
    calls, bodies = [], iter([b"<svg onload='alert(1)'/>", b"\x89PNG\r\n\x1a\n" + b"\0" * read.ICON_CAP])

    def stream(url, ip, headers):
        calls.append(url)
        return _Resp(body=next(bodies), ctype="image/svg+xml")

    monkeypatch.setattr(wf, "_stream", stream)
    assert client.get("/discover/icon/n.test").status_code == 404
    assert client.get("/discover/icon/n.test").status_code == 404 and len(calls) == 1
    marker = store.home() / "icons" / "n.test.none"
    old = time.time() - (read.ICON_RETRY_DAYS + 1) * 86400
    os.utime(marker, (old, old))
    assert client.get("/discover/icon/n.test").status_code == 404 and len(calls) == 2
    assert marker.stat().st_mtime > old and not (store.home() / "icons" / "n.test").exists()
