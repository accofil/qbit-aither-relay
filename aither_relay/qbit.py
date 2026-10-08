"""Minimal qBittorrent Web API client (stdlib only, API v2.11 / qBittorrent 5.x)."""

from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


class QbitError(RuntimeError):
    pass


class Qbit:
    def __init__(self, url: str, username: str, password: str, timeout: int = 60, host_header: str = ""):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        # behind an ssh -L tunnel the local port differs from the WebUI port, and qBittorrent's
        # Host header validation answers 401 unless the Host header names the real port
        self.host_header = host_header
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self._logged_in = False

    # -- low level -----------------------------------------------------------------
    def _request(self, method: str, path: str, data: dict | None = None, *,
                 body: bytes | None = None, content_type: str | None = None, raw: bool = False):
        url = f"{self.url}/api/v2/{path}"
        if method == "GET" and data:
            url += "?" + urllib.parse.urlencode(data)
            data = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            content_type = "application/x-www-form-urlencoded"
        origin = f"http://{self.host_header}" if self.host_header else self.url
        headers = {"Referer": origin, "Origin": origin}
        if self.host_header:
            headers["Host"] = self.host_header
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and path != "auth/login":
                raise PermissionError("forbidden") from exc
            raise QbitError(f"{method} {path}: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise QbitError(f"{method} {path}: {exc}") from exc
        if raw:
            return payload
        text = payload.decode("utf-8", "replace")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def login(self) -> None:
        res = self._request("POST", "auth/login", {"username": self.username, "password": self.password})
        if res != "Ok.":
            raise QbitError(f"login failed at {self.url}: {res!r}")
        self._logged_in = True

    def call(self, method: str, path: str, data: dict | None = None, **kw):
        if not self._logged_in:
            self.login()
        try:
            return self._request(method, path, data, **kw)
        except PermissionError:
            self.login()
            return self._request(method, path, data, **kw)

    # -- high level ----------------------------------------------------------------
    def version(self) -> str:
        return self.call("GET", "app/version")

    def torrents(self, **filters) -> list[dict]:
        return self.call("GET", "torrents/info", filters or None) or []

    def torrent(self, h: str) -> dict | None:
        res = self.torrents(hashes=h)
        return res[0] if res else None

    def trackers(self, h: str) -> list[dict]:
        return self.call("GET", "torrents/trackers", {"hash": h}) or []

    def files(self, h: str) -> list[dict]:
        return self.call("GET", "torrents/files", {"hash": h}) or []

    def export(self, h: str) -> bytes:
        return self.call("GET", "torrents/export", {"hash": h}, raw=True)

    def set_location(self, h: str, location: str) -> None:
        self.call("POST", "torrents/setLocation", {"hashes": h, "location": location})

    def recheck(self, h: str) -> None:
        self.call("POST", "torrents/recheck", {"hashes": h})

    def start(self, h: str) -> None:
        self.call("POST", "torrents/start", {"hashes": h})

    def stop(self, h: str) -> None:
        self.call("POST", "torrents/stop", {"hashes": h})

    def delete(self, h: str, delete_files: bool) -> None:
        self.call("POST", "torrents/delete", {"hashes": h, "deleteFiles": "true" if delete_files else "false"})

    def add_tags(self, h: str, tags: str) -> None:
        self.call("POST", "torrents/addTags", {"hashes": h, "tags": tags})

    def remove_tags(self, h: str, tags: str) -> None:
        self.call("POST", "torrents/removeTags", {"hashes": h, "tags": tags})

    def ensure_category(self, name: str) -> None:
        if name and name not in (self.call("GET", "torrents/categories") or {}):
            self.call("POST", "torrents/createCategory", {"category": name, "savePath": ""})

    def add_torrent_file(self, torrent: bytes, filename: str, fields: dict[str, str]) -> None:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/x-bittorrent\r\n\r\n".encode() + torrent + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        res = self.call("POST", "torrents/add", body=b"".join(parts),
                        content_type=f"multipart/form-data; boundary={boundary}")
        if res not in (None, "Ok."):
            raise QbitError(f"add torrent failed: {res!r}")
