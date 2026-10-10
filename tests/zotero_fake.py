# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A fake Zotero local API for tests: real JSON captured from Zotero 10.0.5 on
2026-10-05 (tests/fixtures/zotero), served through httpx.MockTransport so the
adapter's own HTTP code runs unmocked."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "zotero"
SERVER_ID = "dc8XI1gaCC7j"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeZotero:
    """Items by key, collections by key, and a log of every request."""

    def __init__(self, server_id: str = SERVER_ID, library_version: int = 3311):
        self.server_id = server_id
        self.library_version = library_version
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.files: dict[str, str] = {}       # attachment key -> file:// url
        self.requests: list[httpx.Request] = []
        self.refuse = False
        self.api_off = False   # Zotero up with "Allow other applications" unticked
        self.zotero_version = "10.0.5"
        self.api_key = "k" * 32
        self.authorize_answer: dict | None = {"key": "k" * 32, "remember": True}
        self._n = 0

    def _new_key(self) -> str:
        self._n += 1
        return f"NEW{self._n:05d}"

    def _bump(self, obj: dict) -> None:
        self.library_version += 1
        obj["version"] = obj["data"]["version"] = self.library_version

    def _write_guard(self, request: httpx.Request, obj: dict | None = None) -> httpx.Response | None:
        """The local API's write rules: server id required (428), a key (403),
        and the object's version when the client names one (412)."""
        if request.headers.get("Zotero-Server-ID") != self.server_id:
            return httpx.Response(428)
        if request.headers.get("Zotero-API-Key") != self.api_key:
            return httpx.Response(403)
        want = request.headers.get("If-Unmodified-Since-Version")
        if obj is not None and want is not None and int(want) != obj["version"]:
            return httpx.Response(412)
        return None

    def note(self, parent: str, html: str, tags=("silica",)) -> dict:
        key = self._new_key()
        self.library_version += 1
        return self.add({"key": key, "version": self.library_version, "links": {}, "meta": {},
                         "data": {"key": key, "version": self.library_version, "itemType": "note",
                                  "parentItem": parent, "note": html,
                                  "tags": [{"tag": t} for t in tags], "collections": [],
                                  "relations": {}}})

    def collection(self, name: str, parent: str | bool = False) -> dict:
        key = self._new_key()
        self.library_version += 1
        col = {"key": key, "version": self.library_version, "meta": {},
               "data": {"key": key, "version": self.library_version, "name": name,
                        "parentCollection": parent, "relations": {}}}
        self.collections[key] = col
        return col

    def add(self, item: dict) -> dict:
        self.items[item["key"]] = item
        return item

    def add_paper(self) -> tuple[dict, dict]:
        item = self.add(fixture("item_8H4AC8GW.json"))
        att = self.add(fixture("attachment_QH2BR53F.json"))
        self.files["QH2BR53F"] = (
            "file:///home/user/Zotero/storage/QH2BR53F/"
            "Packer%20et%20al.%20-%202024%20-%20MemGPT%20Towards%20LLMs%20as%20Operating%20Systems.pdf"
        )
        return item, att

    def clone(self, item: dict, key: str, **data) -> dict:
        new = copy.deepcopy(item)
        new["key"] = new["data"]["key"] = key
        new["data"].update(data)
        return self.add(new)

    def _json(self, body, total: int | None = None) -> httpx.Response:
        headers = {"Zotero-Server-ID": self.server_id,
                   "Last-Modified-Version": str(self.library_version)}
        if total is not None:
            headers["Total-Results"] = str(total)
        return httpx.Response(200, json=body, headers=headers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.refuse:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/local/authorize" and request.method == "POST":
            if request.headers.get("Zotero-Server-ID") != self.server_id:
                return httpx.Response(428)
            if self.authorize_answer is None:
                return httpx.Response(403)
            return httpx.Response(200, json=self.authorize_answer,
                                  headers={"Zotero-Server-ID": self.server_id})
        prefix = "/api/users/0"
        if self.api_off and path.startswith("/api/"):
            return httpx.Response(403, text="Local API is not enabled")
        if path == "/api/":
            r = self._json({})
            r.headers["X-Zotero-Version"] = self.zotero_version
            return r
        if not path.startswith(prefix):
            return httpx.Response(404)
        if request.method != "GET":
            return self._write(request)
        rest = path[len(prefix):]
        parts = rest.strip("/").split("/")
        top = [i for i in self.items.values() if not i["data"].get("parentItem")]
        if parts == ["items", "top"]:
            return self._page(request, top)
        if len(parts) == 2 and parts[0] == "items":
            item = self.items.get(parts[1])
            return self._json(item) if item else httpx.Response(404)
        if len(parts) == 5 and parts[0] == "items" and parts[2:] == ["file", "view", "url"]:
            url = self.files.get(parts[1])
            if not url:
                return httpx.Response(404)
            return httpx.Response(200, text=url, headers={"Zotero-Server-ID": self.server_id})
        if len(parts) == 4 and parts[0] == "collections" and parts[2:] == ["items", "top"]:
            members = [i for i in top if parts[1] in i["data"].get("collections", [])]
            return self._page(request, members)
        if parts == ["collections"]:
            return self._json(list(self.collections.values()))
        if len(parts) == 2 and parts[0] == "collections":
            col = self.collections.get(parts[1])
            return self._json(col) if col else httpx.Response(404)
        if len(parts) == 3 and parts[0] == "collections" and parts[2] == "collections":
            subs = [c for c in self.collections.values()
                    if c["data"]["parentCollection"] == parts[1]]
            return self._json(subs, total=len(subs))
        if len(parts) == 3 and parts[0] == "items" and parts[2] == "children":
            kids = [i for i in self.items.values() if i["data"].get("parentItem") == parts[1]]
            return self._json(kids, total=len(kids))
        if parts == ["items"] and "since" in request.url.params:
            since = int(request.url.params["since"])
            return self._json({k: i["version"] for k, i in self.items.items() if i["version"] > since})
        if parts == ["items"]:
            tag = request.url.params.get("tag")
            hits = [i for i in self.items.values()
                    if not tag or tag in [t["tag"] for t in i["data"].get("tags", [])]]
            return self._page(request, hits)
        return httpx.Response(404)

    def _write(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path[len("/api/users/0"):].strip("/").split("/")
        body = json.loads(request.content or b"null")
        if request.method == "POST" and parts == ["items"]:
            denied = self._write_guard(request)
            if denied:
                return denied
            ok = {}
            for i, obj in enumerate(body):
                made = self.note(obj["parentItem"], obj["note"],
                                 [t["tag"] for t in obj.get("tags", [])])
                ok[str(i)] = {"key": made["key"], "version": made["version"]}
            return httpx.Response(200, json={"successful": ok, "success": {
                k: v["key"] for k, v in ok.items()}, "unchanged": {}, "failed": {}},
                headers={"Zotero-Server-ID": self.server_id})
        if request.method == "POST" and parts == ["collections"]:
            denied = self._write_guard(request)
            if denied:
                return denied
            ok = {}
            for i, obj in enumerate(body):
                made = self.collection(obj["name"], obj.get("parentCollection", False))
                ok[str(i)] = {"key": made["key"], "version": made["version"]}
            return httpx.Response(200, json={"successful": ok, "success": {
                k: v["key"] for k, v in ok.items()}, "unchanged": {}, "failed": {}},
                headers={"Zotero-Server-ID": self.server_id})
        store = self.items if parts[0] == "items" else self.collections
        obj = store.get(parts[1]) if len(parts) == 2 else None
        if obj is None:
            return httpx.Response(404)
        denied = self._write_guard(request, obj)
        if denied:
            return denied
        if request.method == "PATCH":
            obj["data"].update(body)
            self._bump(obj)
            return httpx.Response(204, headers={"Zotero-Server-ID": self.server_id,
                                                "Last-Modified-Version": str(obj["version"])})
        if request.method == "DELETE":
            del store[parts[1]]
            self.library_version += 1
            return httpx.Response(204, headers={"Zotero-Server-ID": self.server_id})
        return httpx.Response(405)

    def _page(self, request: httpx.Request, items: list[dict]) -> httpx.Response:
        start = int(request.url.params.get("start", 0))
        limit = int(request.url.params.get("limit", 25))
        return self._json(items[start:start + limit], total=len(items))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
