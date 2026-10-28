# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Reading one feed: RSS, Atom and JSON Feed into articles (ADR-0137 point 3).

The standard library only, as hermes-agent's `rss-feeds` skill reads them:
ElementTree for the XML (expat 2.7 bounds entity amplification and fetches no
external entity, and `web_fetch._read_capped` bounds the bytes before it), and
html.parser for the markup inside a description. Every value that comes out is
untrusted text from a publisher (ADR-0009): it is shown, fenced and cited,
never followed as an instruction.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC = "{http://purl.org/dc/elements/1.1/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
RDF = "{http://purl.org/rss/1.0/}"

# What a card and the grouping read of an article: its first lines. A feed's
# description can be the whole article (Wired, NYT), and nothing here needs it.
LEAD_CHARS = 400
_IMG_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|[?#])", re.I)
# A feed's own logo or a site-wide placeholder rides every item with the same
# URL. Measured 2026-10-28 on ten feeds: no real cover repeated more than twice
# in one feed, so three is where an image stops being an article's.
LOGO_REPEATS = 3


def article_id(url: str) -> str:
    """Stable id of an article: its link, hashed, so the store can key on it."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class _Text(HTMLParser):
    """Visible text of a description, and the first `<img src>` in it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.img = ""
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "img" and not self.img:
            self.img = (dict(attrs).get("src") or "").strip()
        elif tag in ("p", "br", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_text(markup: str) -> tuple[str, str]:
    """(plain text, first image src) of a description that may hold markup."""
    p = _Text()
    try:
        p.feed(markup or "")
        p.close()
    except Exception:
        # html.parser raises on very little, but a publisher's markup is not
        # ours to trust: unparseable markup is read as the bare text it holds.
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", markup or "")).split()), ""
    return " ".join("".join(p.parts).split()), p.img


def _lead(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= LEAD_CHARS:
        return text
    cut = text[:LEAD_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _when(raw: str | None) -> float | None:
    """A feed date (RFC 822 or ISO 8601) as a timestamp; None when unreadable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _txt(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def item_image(item: ET.Element, body: str = "") -> str:
    """The cover an item declares, in ADR-0137 point 7's order.

    `media:content` with an image type, then `media:thumbnail`, then an image
    `enclosure`, then the first `<img>` of the body. A media:content with no
    type counts when its URL names an image file, which is how CNN and the
    Guardian write it. The order is decided per item, not per tag seen first:
    BBC lists a thumbnail before any content element.
    """
    content = thumb = enclosure = ""
    for el in item.iter():
        tag = el.tag
        url = (el.get("url") or "").strip()
        if not url:
            continue
        if tag == MEDIA + "content" and not content:
            kind = (el.get("type") or "").lower()
            if el.get("medium") == "image" or kind.startswith("image/") or (not kind and _IMG_EXT.search(url)):
                content = url
        elif tag == MEDIA + "thumbnail" and not thumb:
            thumb = url
        elif _local(tag) == "enclosure" and not enclosure and (el.get("type") or "").lower().startswith("image/"):
            enclosure = url
    if content or thumb or enclosure:
        return content or thumb or enclosure
    return html_text(body)[1] if body else ""


def _absolute(url: str, base: str) -> str:
    # An empty href joins to the base itself: an item with no image would
    # come back with its own link as its cover.
    if not (url or "").strip():
        return ""
    url = urljoin(base, url.strip())
    return url if urlsplit(url).scheme in ("http", "https") else ""


def parse(data: bytes, feed_url: str) -> tuple[str, list[dict]]:
    """(the feed's title, its articles) from a feed body.

    Raises ValueError when the body is neither XML nor a JSON Feed. Articles
    carry: url, title, lead, published (timestamp or None), image, authors,
    and for a video its id and duration.
    """
    head = data.lstrip()[:1]
    if head == b"{":
        return _parse_json(data, feed_url)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not a feed: {e}") from None
    kind = _local(root.tag).lower()
    if kind == "feed":
        return _parse_atom(root, feed_url)
    if kind in ("rss", "rdf"):
        return _parse_rss(root, feed_url)
    raise ValueError(f"not a feed: the document is <{kind}>")


def _parse_rss(root: ET.Element, feed_url: str) -> tuple[str, list[dict]]:
    channel = root.find("channel")
    title = _txt(channel.find("title")) if channel is not None else ""
    items = root.findall(".//item") or root.findall(f".//{RDF}item")
    out = []
    for it in items:
        def f(name: str) -> ET.Element | None:
            return it.find(name) if it.find(name) is not None else it.find(RDF + name)
        link = _absolute(_txt(f("link")) or (it.findtext("guid") or ""), feed_url)
        if not link:
            continue
        desc = _txt(f("description"))
        body = it.findtext(CONTENT) or desc
        text, _ = html_text(desc or body)
        authors = [a.strip() for a in (_txt(it.find(DC + "creator")) or _txt(f("author"))).split(",") if a.strip()]
        out.append({
            "url": link, "title": html_text(_txt(f("title")))[0],
            "lead": _lead(text), "published": _when(it.findtext("pubDate") or it.findtext(DC + "date")),
            "image": _absolute(item_image(it, body), link), "authors": authors[:6],
        })
    return title, out


def _parse_atom(root: ET.Element, feed_url: str) -> tuple[str, list[dict]]:
    title = _txt(root.find(ATOM + "title"))
    out = []
    for e in root.findall(ATOM + "entry"):
        link = ""
        for ln in e.findall(ATOM + "link"):
            if (ln.get("rel") or "alternate") == "alternate" and ln.get("href"):
                link = ln.get("href") or ""
                break
        link = _absolute(link, feed_url)
        if not link:
            continue
        group = e.find(MEDIA + "group")
        summary = _txt(e.find(ATOM + "summary")) or _txt(e.find(ATOM + "content"))
        if group is not None and not summary:
            summary = _txt(group.find(MEDIA + "description"))
        text, _ = html_text(summary)
        art = {
            "url": link, "title": html_text(_txt(e.find(ATOM + "title")))[0], "lead": _lead(text),
            "published": _when(e.findtext(ATOM + "published") or e.findtext(ATOM + "updated")),
            "image": _absolute(item_image(e, _txt(e.find(ATOM + "content"))), link),
            "authors": [_txt(a.find(ATOM + "name")) for a in e.findall(ATOM + "author")][:6],
        }
        if (vid := e.findtext(YT + "videoId")):
            art["video"] = vid.strip()
        out.append(art)
    return title, out


def _parse_json(data: bytes, feed_url: str) -> tuple[str, list[dict]]:
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise ValueError(f"not a feed: {e}") from None
    if not isinstance(doc, dict) or "jsonfeed.org" not in str(doc.get("version", "")):
        raise ValueError("not a feed: JSON without a JSON Feed version")
    out = []
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        link = _absolute(str(it.get("url") or it.get("external_url") or ""), feed_url)
        if not link:
            continue
        body = str(it.get("content_html") or "")
        text = str(it.get("summary") or it.get("content_text") or "") or html_text(body)[0]
        image = str(it.get("image") or it.get("banner_image") or "") or html_text(body)[1]
        authors = [str(a.get("name")) for a in it.get("authors") or [] if isinstance(a, dict) and a.get("name")]
        out.append({"url": link, "title": " ".join(str(it.get("title") or "").split()), "lead": _lead(text),
                    "published": _when(str(it.get("date_published") or "")),
                    "image": _absolute(image, link), "authors": authors[:6]})
    return str(doc.get("title") or ""), out


def drop_logos(articles: list[dict]) -> None:
    """Clear an image URL that LOGO_REPEATS or more articles of one feed share.

    A logo is not a cover (ADR-0137 point 7). Per feed, because two outlets
    can legitimately run the same agency photo.
    """
    counts: dict[str, int] = {}
    for a in articles:
        if a.get("image"):
            counts[a["image"]] = counts.get(a["image"], 0) + 1
    for a in articles:
        if counts.get(a.get("image") or "", 0) >= LOGO_REPEATS:
            a["image"] = ""


# --- what a typed address is ---------------------------------------------------

_ARXIV_CAT = re.compile(r"^(?:arxiv:)?([a-z\-]+(?:\.[A-Z]{2})?)$")
_YT_HANDLE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]+|channel/UC[\w\-]{22})", re.I)


def classify(raw: str) -> tuple[str, str]:
    """(kind, address) for what the person typed in Add a feed.

    `arxiv` takes a bare category (cs.CC, math.CO, astro-ph) to its listing,
    `youtube` a channel, `wikipedia` an encyclopedia's host, and `url`
    everything else, whose feed is found by `discover_links`.
    """
    s = raw.strip()
    m = _ARXIV_CAT.match(s)
    if m and "." in m.group(1) or s.lower().startswith("arxiv:"):
        return "arxiv", f"https://rss.arxiv.org/rss/{m.group(1) if m else s.split(':', 1)[1]}"
    if "rss.arxiv.org" in s or "export.arxiv.org/rss" in s:
        return "arxiv", s if "://" in s else "https://" + s
    if _YT_HANDLE.match(s):
        return "youtube", s if "://" in s else "https://" + s
    host = urlsplit(s if "://" in s else "https://" + s).hostname or ""
    if host.endswith("wikipedia.org"):
        return "wikipedia", f"https://{host}"
    return "url", s if "://" in s else "https://" + s


class _Alternates(HTMLParser):
    """`<link rel="alternate">` feeds a page names in its head."""

    TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json", "application/json")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = dict(attrs)
        rel = (a.get("rel") or "").lower().split()
        if "alternate" in rel and (a.get("type") or "").lower() in self.TYPES and a.get("href"):
            self.found.append(a["href"])
        elif "canonical" in rel and a.get("href") and not self.canonical:
            self.canonical = a["href"]


def discover_links(html: str, base: str) -> list[str]:
    """The feeds a page declares, absolute, in the order it lists them, then
    the conventional paths to try when it declares none (ADR-0137 point 3)."""
    p = _Alternates()
    try:
        p.feed(html)
        p.close()
    except Exception:
        # A page we cannot parse declares nothing; the conventional paths below
        # are still worth a try.
        pass
    declared = [u for u in (_absolute(h, base) for h in p.found) if u]
    root = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    guesses = [root + path for path in ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")]
    return list(dict.fromkeys(declared + guesses))


def parse_opml(data: bytes) -> list[dict]:
    """[{url, name, tags}] of every feed an OPML file lists; a feed's tags are
    the outline folders it sits in."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not an OPML file: {e}") from None
    body = root.find("body")
    if _local(root.tag).lower() != "opml" or body is None:
        raise ValueError("not an OPML file")
    out: list[dict] = []

    def walk(node: ET.Element, folders: list[str]) -> None:
        for o in node.findall("outline"):
            url = (o.get("xmlUrl") or "").strip()
            name = (o.get("title") or o.get("text") or "").strip()
            if url:
                out.append({"url": url, "name": name, "tags": folders[-2:]})
            else:
                walk(o, folders + ([name] if name else []))

    walk(body, [])
    return out


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
