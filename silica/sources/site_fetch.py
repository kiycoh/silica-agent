# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A Reddit thread and a GitHub repository, file, issue or pull request, read
for `/fetch` from the site's own JSON (ADR-0162), in the YouTube branch's place
in `web_fetch.fetch_page`.

To a reader without a browser both pages are walls or shells: Reddit answers a
reader with no session with a 403 page (this laptop, 2026-11-12), and GitHub's
issue page carries its comments in a script. The JSON is the thread and the
file themselves, in order, with authors and dates, so the note is written from
it and keeps a fetched page's shape: the `Source:` line, then the text.

The text stays under `web_fetch`'s character ceiling, the one every fetched
page has, because `fetch_page` also feeds the model (`web_fetch`, check,
Discover). The ceiling is applied here, a comment at a time, so a note never
ends inside a comment and says how many it left out.
"""
from __future__ import annotations

import base64
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from silica import __version__
from silica.sources import web_fetch as wf

# A reader that names Silica and nobody else (ADR-0162 point 3): no account,
# no address, and no handle of the person using it.
HEADERS = {"User-Agent": f"silica-agent/{__version__.split('+')[0]}", "Accept": "application/json"}
# What the head of a thread may take before its comments: the count line and
# the `Source:` line `_render` puts on top fit in the rest.
_HEAD_ROOM = 400

REDDIT_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"})
# A post, or a comment under it: /r/<sub>/comments/<id>/<slug>/<comment>/.
_REDDIT_POST = re.compile(r"/(?:r/[^/]+/)?comments/\w+(?:/[^/]*(?:/\w+)?)?/?")
_REDDIT_SHORT = re.compile(r"/(\w+)/?")
# Reddit's own bound, asked for and held again here (ADR-0162 point 2).
REDDIT_DEPTH = 3
REDDIT_COMMENTS = 200

GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_GH_NAME = r"[\w.-]+"
_GH_REPO = re.compile(rf"/({_GH_NAME})/({_GH_NAME}?)(?:\.git)?/?")
_GH_BLOB = re.compile(rf"/({_GH_NAME})/({_GH_NAME})/blob/(.+)")
_GH_ISSUE = re.compile(rf"/({_GH_NAME})/({_GH_NAME})/(?:issues|pull)/(\d+)(?:/.*)?")
_GH_API = "https://api.github.com"
_GH_RAW = "https://raw.githubusercontent.com"
# GitHub's largest page of comments; more pages follow while the ceiling has room.
_GH_PAGE = 100
# Set by `fetch_to_inbox` around the person's own /fetch: only an address they
# typed carries their GITHUB_TOKEN. The model's web_fetch, Discover and the
# check reach `fetch_page` too, on addresses a page or a feed named, and a
# token there would let a page's words read the person's private repositories.
PERSON_ASKED: ContextVar[bool] = ContextVar("site_fetch_person_asked", default=False)
_MARKDOWN = (".md", ".markdown", ".mdown", ".mkd")


def page(url: str, headers: dict[str, str] = wf.HEADERS) -> wf.Page | None:
    """The thread or the GitHub page at `url`, or None for any other address,
    which `fetch_page` reads as HTML. Exact hosts: i.redd.it, gist.github.com
    and docs.github.com are other sites. `headers` are `fetch_page`'s, for the
    one HTML page read here."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password:
        return None
    if host in REDDIT_HOSTS and _REDDIT_POST.fullmatch(parts.path):
        return _reddit(url, parts.path.rstrip("/"))
    if host == "redd.it" and (m := _REDDIT_SHORT.fullmatch(parts.path)):
        return _reddit(url, f"/comments/{m[1]}")
    if host in GITHUB_HOSTS:
        return _github(url, parts.path, headers)
    return None


def _day(stamp: object) -> str:
    """A Unix time or an ISO stamp as its UTC date; "" when there is none."""
    try:
        if isinstance(stamp, (int, float)):
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _bounded(head: str, blocks: list[str], total: int, how: str = "") -> str:
    """`head`, then as many of `blocks` as the ceiling holds, under one line
    saying how many of `total` comments they are. The line goes first, so it is
    read before the comments it counts. A fence left open closes where its
    post or comment ends, as the site draws it, or every comment after it
    reads as code."""
    head = wf.close_fence(wf.truncate(head, wf._MAX_CHARS - _HEAD_ROOM))
    room = wf._MAX_CHARS - len(head) - _HEAD_ROOM
    kept: list[str] = []
    for block in map(wf.close_fence, blocks):
        room -= len(block) + 2
        if room < 0:
            break
        kept.append(block)
    total = max(total, len(kept))
    if not total:
        return head
    said = (f"1 comment{how}." if total == len(kept) == 1 else f"All {total:,} comments{how}." if len(kept) == total
            else f"{len(kept):,} of {total:,} comments{how}; {total - len(kept):,} left out.")
    return "\n\n".join([head, f"## Comments\n\n{said}", *kept])


# --- Reddit ---------------------------------------------------------------------

def _reddit_said(status: int) -> str:
    if status == 429:
        return ("Reddit is limiting reads from this address (429): wait a minute and try again. "
                "Nothing was written.")
    return (f"Reddit refused to show this thread ({status}): it turns away readers without a "
            "Reddit session, from some networks every time. Nothing was written.")


def _reddit(url: str, path: str) -> wf.Page:
    """The post and its comments: Reddit's top sort, REDDIT_DEPTH levels and at
    most REDDIT_COMMENTS, in thread order, each with its author and date."""
    query = urlencode({"raw_json": 1, "sort": "top", "depth": REDDIT_DEPTH, "limit": REDDIT_COMMENTS})
    try:
        resp, _ = wf.fetch(f"https://www.reddit.com{path}.json?{query}", HEADERS)
        listing = resp.json()
        post = listing[0]["data"]["children"][0]["data"]
        top = listing[1]["data"]["children"] if len(listing) > 1 else []
    except wf.Refused as exc:
        raise wf.SiteRefused(_reddit_said(exc.status)) from None
    except (ValueError, KeyError, IndexError, TypeError):
        # A login page, a removed thread's stub, anything but the two listings:
        # nothing in it is the thread, so nothing is written from it.
        raise wf.SiteRefused("Reddit did not answer with the thread: it may be private, removed, "
                             "or behind a login. Nothing was written.") from None
    meta = " · ".join(x for x in (
        str(post.get("subreddit_name_prefixed") or ""), f"u/{post.get('author') or '[deleted]'}",
        _day(post.get("created_utc")), f"{post.get('score', 0)} points") if x)
    lines = [meta, f"https://www.reddit.com{post.get('permalink') or path}"]
    if not post.get("is_self") and post.get("url"):
        lines.append(f"Links to: {post['url']}")
    head = "\n".join(lines)
    if selftext := str(post.get("selftext") or "").strip():
        head = f"{head}\n\n{selftext}"
    comments = _reddit_comments(top, 0, "")[:REDDIT_COMMENTS]
    body = _bounded(head, comments, int(post.get("num_comments") or 0),
                    f", top first and {REDDIT_DEPTH} levels deep")
    return wf.Page(wf._render(url, body), " ".join(str(post.get("title") or "").split())[:120])


def _reddit_comments(children: list, depth: int, parent: str) -> list[str]:
    """Each comment as a block, depth first, so a reply follows what it answers.
    A `more` child is the part of the thread Reddit did not send; the count of
    what is left out covers it."""
    out: list[str] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        c = child.get("data") or {}
        author = f"u/{c.get('author') or '[deleted]'}"
        said = f"**{author}**" + (f" replying to {parent}" if parent else "")
        said = " · ".join(x for x in (said, _day(c.get("created_utc")), f"{c.get('score', 0)} points") if x)
        out.append(f"{said}\n{str(c.get('body') or '').strip()}")
        replies = c.get("replies")
        if depth + 1 < REDDIT_DEPTH and isinstance(replies, dict):
            out += _reddit_comments((replies.get("data") or {}).get("children") or [], depth + 1, author)
    return out


# --- GitHub ---------------------------------------------------------------------

def _gh_headers() -> dict[str, str]:
    """GitHub's JSON, with the person's GITHUB_TOKEN when they set one and
    typed the address themselves (PERSON_ASKED). Never on the server: a token
    in the host's environment is the operator's, and every tenant would read
    whatever it can (ADR-0162 point 5)."""
    from silica.config import hosted, live_env

    out = {**HEADERS, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if PERSON_ASKED.get() and (token := live_env("GITHUB_TOKEN")) and not hosted():
        out["Authorization"] = f"Bearer {token}"
    return out


def _token_hint(said: str) -> str:
    """`said` on a laptop, where the person can set GITHUB_TOKEN; "" on the
    server, whose tenants cannot and whose reads never carry one."""
    from silica.config import hosted

    return "" if hosted() else said


def _gh_fetch(url: str) -> httpx.Response | None:
    """One read from GitHub, None on a 404; a refusal says which it was."""
    try:
        resp, _ = wf.fetch(url, _gh_headers())
        return resp
    except wf.Refused as exc:
        if exc.status == 429 or exc.headers.get("x-ratelimit-remaining") == "0":
            raise wf.SiteRefused("GitHub's limit on reads without a token is spent (60 an hour): "
                                 + _token_hint("set GITHUB_TOKEN, or ")
                                 + "try again later. Nothing was written.") from None
        if exc.status == 401:
            raise wf.SiteRefused("GitHub refused the token in GITHUB_TOKEN (401). Nothing was written.") from None
        raise wf.SiteRefused(f"GitHub refused to show it ({exc.status}). Nothing was written.") from None
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def _not_there(what: str) -> wf.SiteRefused:
    return wf.SiteRefused(f"GitHub has no {what} there, or it is private"
                          + _token_hint(": a private one needs GITHUB_TOKEN") + ". Nothing was written.")


def _gh_found(url: str, what: str) -> httpx.Response:
    if (resp := _gh_fetch(url)) is None:
        raise _not_there(what)
    return resp


def _github(url: str, path: str, headers: dict[str, str]) -> wf.Page | None:
    if m := _GH_ISSUE.fullmatch(path):
        return _gh_issue(url, m[1], m[2], m[3])
    if m := _GH_BLOB.fullmatch(path):
        return _gh_blob(url, m[1], m[2], m[3])
    if m := _GH_REPO.fullmatch(path):
        return _gh_repo(url, m[1], m[2], headers)
    return None


def _verbatim(name: str, text: str, room: int) -> str:
    """A file as the note keeps it: Markdown as itself, anything else in a
    fence longer than any run of backticks inside it, so no line of the file
    can close it. Cut to `room` with the marker outside the fence."""
    if name.lower().endswith(_MARKDOWN):
        return wf.truncate(text, room)
    cut = len(text) > room
    body = text[:room].rstrip() if cut else text.rstrip()
    fence = "`" * (max((len(r) for r in re.findall(r"`{3,}", body)), default=2) + 1)
    lang = name.rpartition(".")[2] if "." in name.strip(".") else ""
    out = f"{fence}{lang}\n{body}\n{fence}"
    return f"{out}\n\n[truncated at {room} characters]" if cut else out


def _gh_repo(url: str, owner: str, repo: str, headers: dict[str, str]) -> wf.Page:
    """Description, topics and the README. On the API's 404 the page itself:
    /features/copilot is a page of GitHub's, not a repository, and its HTML is
    what was asked for; a page that is a 404 too is a repository that does not
    exist or that this reader may not see."""
    resp = _gh_fetch(f"{_GH_API}/repos/{owner}/{repo}")
    if resp is None:
        try:
            return wf.html_page(url, headers)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise _not_there("repository") from None
            raise
    info = resp.json()
    name = str(info.get("full_name") or f"{owner}/{repo}")
    lines = [str(info.get("description") or "").strip()]
    if topics := info.get("topics"):
        lines.append("Topics: " + ", ".join(map(str, topics)))
    lines.append(str(info.get("html_url") or f"https://github.com/{name}"))
    head = "\n".join(x for x in lines if x)
    if (readme := _gh_fetch(f"{_GH_API}/repos/{owner}/{repo}/readme")) is not None:
        doc = readme.json()
        text = base64.b64decode(str(doc.get("content") or "")).decode("utf-8", "replace")
        file = str(doc.get("name") or "README")
        head = f"{head}\n\n## {file}\n\n{_verbatim(file, text, wf._MAX_CHARS - len(head) - _HEAD_ROOM)}"
    return wf.Page(wf._render(url, head), name)


def _gh_blob(url: str, owner: str, repo: str, rest: str) -> wf.Page:
    """The file, raw. raw.githubusercontent.com takes the branch and the path
    as the page names them, so a branch with a slash in it is resolved there
    and not guessed here; the type gate refuses a picture or a binary."""
    resp = _gh_found(f"{_GH_RAW}/{owner}/{repo}/{rest}", "file")
    name = rest.rsplit("/", 1)[-1]
    head = f"{owner}/{repo}: {rest}"
    text = _verbatim(name, resp.text, wf._MAX_CHARS - len(head) - _HEAD_ROOM)
    return wf.Page(wf._render(url, f"{head}\n\n{text}"), f"{name} ({owner}/{repo})")


def _gh_issue(url: str, owner: str, repo: str, number: str) -> wf.Page:
    """An issue or a pull request and its conversation in order: the comments,
    never the diff or its review notes, which belong to lines of code."""
    base = f"{_GH_API}/repos/{owner}/{repo}/issues/{number}"
    issue = _gh_found(base, "issue or pull request").json()
    pr = issue.get("pull_request")
    reason = str(issue.get("state_reason") or "")
    state = ("merged" if isinstance(pr, dict) and pr.get("merged_at")
             else "open" if issue.get("state") == "open"
             else "closed as " + reason.replace("_", " ") if reason in ("completed", "not_planned")
             else "closed")
    who = (issue.get("user") or {}).get("login") or "ghost"
    head = (f"{'Pull request' if pr else 'Issue'} #{number} in {owner}/{repo} · {state} · "
            f"opened by @{who} on {_day(issue.get('created_at'))}\n{issue.get('html_url') or url}")
    if body := str(issue.get("body") or "").strip():
        head = f"{head}\n\n{body}"
    total = int(issue.get("comments") or 0)
    blocks: list[str] = []
    room, n = wf._MAX_CHARS - len(head), 1
    while len(blocks) < total and room > 0:
        rows = _gh_found(f"{base}/comments?{urlencode({'per_page': _GH_PAGE, 'page': n})}",
                         "issue or pull request").json()
        for c in rows:
            blocks.append(f"**@{(c.get('user') or {}).get('login') or 'ghost'}** · {_day(c.get('created_at'))}\n"
                          f"{str(c.get('body') or '').strip()}")
            room -= len(blocks[-1])
        if len(rows) < _GH_PAGE:
            break
        n += 1
    return wf.Page(wf._render(url, _bounded(head, blocks, total)),
                   " ".join(str(issue.get("title") or "").split())[:120])
