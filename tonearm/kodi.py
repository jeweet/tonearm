"""Kodi JSON-RPC client (HTTP) and TCP notification listener.

Pure Python / stdlib only so it can be tested without GTK.
"""
from __future__ import annotations

import base64
import http.client
import itertools
import json
import os
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from .logger import log

_DEBUG = bool(os.environ.get("TONEARM_DEBUG"))  # log every JSON-RPC call


class KodiError(Exception):
    """Kodi answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message
        self.data = data


class KodiConnectionError(Exception):
    """Kodi could not be reached."""


CONNECT_TIMEOUT = 5.0   # seconds to open a connection (Kodi on the LAN answers in ms)


class KodiTimeout(KodiConnectionError):
    """Kodi accepted the request but didn't answer in time (usually just busy)."""


INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


@dataclass
class Instance:
    """A configured Kodi instance."""

    id: str
    name: str
    host: str
    port: int = 8080
    tcp_port: int = 9090
    username: str = "kodi"
    password: str = ""
    https: bool = False
    mac: str = ""          # for Wake-on-LAN (learnt from Kodi while connected)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "host": self.host,
            "port": self.port, "tcp_port": self.tcp_port,
            "username": self.username, "https": self.https, "mac": self.mac,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Instance":
        return cls(
            id=d["id"], name=d.get("name") or d["host"], host=d["host"],
            port=int(d.get("port", 8080)), tcp_port=int(d.get("tcp_port", 9090)),
            username=d.get("username", "kodi"), password=d.get("password", ""),
            https=bool(d.get("https", False)), mac=d.get("mac", "") or "",
        )


class KodiClient:
    """Thread-safe JSON-RPC over HTTP client.

    Keeps one persistent HTTP connection per thread (cheap on mobile radios).
    """

    def __init__(self, instance: Instance, timeout: float = 15.0):
        self.instance = instance
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._local = threading.local()
        self.api_version: tuple[int, int, int] | None = None

    # -- transport -------------------------------------------------------
    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            cls = http.client.HTTPSConnection if self.instance.https else http.client.HTTPConnection
            conn = cls(self.instance.host, self.instance.port, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def _drop_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.instance.username or self.instance.password:
            tok = f"{self.instance.username}:{self.instance.password}".encode()
            h["Authorization"] = "Basic " + base64.b64encode(tok).decode()
        return h

    def _post(self, payload, timeout: float | None = None) -> object:
        if not _DEBUG:
            return self._post_raw(payload, timeout)
        what = ",".join(p.get("method", "?") for p in payload) if isinstance(payload, list) \
            else payload.get("method", "?")
        t0 = time.monotonic()
        try:
            res = self._post_raw(payload, timeout)
        except Exception as exc:
            log.debug("Kodi %s: failed after %.2fs (%s: %s)", what, time.monotonic() - t0,
                      type(exc).__name__, exc)
            raise
        log.debug("Kodi %s: %.2fs", what, time.monotonic() - t0)
        return res

    def _post_raw(self, payload, timeout: float | None = None) -> object:
        body = json.dumps(payload).encode()
        timeout = timeout or self.timeout
        last_exc = None
        for attempt in range(2):  # retry once on a stale keep-alive socket
            conn = self._conn()
            if conn.sock is None:
                # A new connection: a machine that's switched off never answers, so
                # don't wait the whole request timeout — and call it unreachable, not
                # "busy" (a busy Kodi still accepts connections)
                conn.timeout = min(timeout, CONNECT_TIMEOUT)
                try:
                    conn.connect()
                except (TimeoutError, socket.timeout) as exc:
                    self._drop_conn()
                    raise KodiConnectionError(
                        f"Cannot reach {self.instance.host}:{self.instance.port}: "
                        "no answer") from exc
                except OSError as exc:
                    last_exc = exc
                    self._drop_conn()
                    continue
            conn.timeout = timeout
            conn.sock.settimeout(timeout)
            try:
                conn.request("POST", "/jsonrpc", body=body, headers=self._headers())
                resp = conn.getresponse()
                data = resp.read()
            except (TimeoutError, socket.timeout) as exc:
                # Never re-send a slow request: that only doubles the load on a busy Kodi.
                self._drop_conn()
                raise KodiTimeout(f"Kodi didn't answer within {timeout:.0f} s") from exc
            except (OSError, http.client.HTTPException) as exc:
                last_exc = exc
                self._drop_conn()
                continue
            if resp.status == 401:
                raise KodiConnectionError("Authentication failed (check username/password)")
            if resp.status != 200:
                raise KodiConnectionError(f"HTTP {resp.status} from Kodi")
            try:
                return json.loads(data)
            except ValueError as exc:
                raise KodiConnectionError(f"Invalid response from Kodi: {exc}") from exc
        raise KodiConnectionError(f"Cannot reach {self.instance.host}:{self.instance.port}: {last_exc}")

    # -- API -------------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        req = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params:
            req["params"] = params
        resp = self._post(req, timeout)
        if "error" in resp:
            e = resp["error"]
            raise KodiError(e.get("code", 0), e.get("message", "error"), e.get("data"))
        return resp.get("result")

    def batch(self, calls: list[tuple[str, dict | None]], timeout: float | None = None) -> list:
        """Run several calls in one HTTP round-trip.

        Returns results in order; failed entries are KodiError instances.
        """
        if not calls:
            return []
        reqs = []
        for method, params in calls:
            r = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
            if params:
                r["params"] = params
            reqs.append(r)
        resp = self._post(reqs, timeout)
        if isinstance(resp, dict):  # some errors come back as a single object
            resp = [resp]
        by_id = {r.get("id"): r for r in resp}
        out = []
        for r in reqs:
            item = by_id.get(r["id"])
            if item is None:
                out.append(KodiError(0, "missing batch response"))
            elif "error" in item:
                e = item["error"]
                out.append(KodiError(e.get("code", 0), e.get("message", "error"), e.get("data")))
            else:
                out.append(item.get("result"))
        return out

    def get_api_version(self) -> tuple[int, int, int]:
        if self.api_version is None:
            v = self.call("JSONRPC.Version")["version"]
            self.api_version = (int(v.get("major", 0)), int(v.get("minor", 0)), int(v.get("patch", 0)))
        return self.api_version

    def ping(self, timeout: float = 6.0) -> bool:
        try:
            return self.call("JSONRPC.Ping", timeout=timeout) == "pong"
        except KodiTimeout:
            return True  # it answered the connection, just slowly: it's there
        except (KodiConnectionError, KodiError):
            return False

    def info(self) -> dict:
        api = self.get_api_version()
        app = self.call("Application.GetProperties", {"properties": ["name", "version"]})
        v = app.get("version", {})
        return {
            "name": app.get("name", "Kodi"),
            "version": f"{v.get('major', '?')}.{v.get('minor', '?')}"
                       + (f" {v.get('tag')}" if v.get("tag") and v.get("tag") != "stable" else ""),
            "codename": v.get("revision", ""),
            "api": ".".join(map(str, api)),
        }

    def mac_address(self) -> str:
        """The Kodi machine's MAC address ("" when Kodi can't tell)."""
        from .wol import normalize_mac  # noqa: PLC0415
        labels = self.call("XBMC.GetInfoLabels", {"labels": ["Network.MacAddress"]})
        return normalize_mac((labels or {}).get("Network.MacAddress")) or ""

    def image_url(self, kodi_path: str) -> str | None:
        """Turn an image://… path into an HTTP URL on the Kodi web server."""
        if not kodi_path:
            return None
        return f"{self.instance.base_url}/image/{urllib.parse.quote(kodi_path, safe='')}"

    def fetch_bytes(self, url: str) -> bytes:
        """GET a URL from Kodi (artwork) with the instance's auth, over a kept-alive
        per-thread connection (no new TCP handshake for every cover)."""
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        headers = self._headers()
        headers.pop("Content-Type", None)
        headers["Accept"] = "image/*"
        last = None
        for _ in range(2):
            conn = getattr(self._local, "img_conn", None)
            if conn is None:
                cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                conn = cls(parsed.hostname, parsed.port, timeout=30)
                self._local.img_conn = conn
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (TimeoutError, socket.timeout) as exc:
                conn.close()
                self._local.img_conn = None
                raise KodiTimeout("artwork download timed out") from exc
            except (OSError, http.client.HTTPException) as exc:  # stale keep-alive: retry once
                last = exc
                conn.close()
                self._local.img_conn = None
                continue
            if resp.status != 200:
                raise KodiConnectionError(f"HTTP {resp.status} fetching artwork")
            return data
        raise KodiConnectionError(str(last))


@dataclass
class _Backoff:
    delay: float = 1.0
    maximum: float = 30.0

    def next(self) -> float:
        d = self.delay
        self.delay = min(self.maximum, self.delay * 2)
        return d

    def reset(self):
        self.delay = 1.0


class NotificationListener:
    """Listens on Kodi's raw TCP JSON-RPC port (default 9090) for events.

    `on_event(method, params)` and `on_state(connected: bool)` are invoked
    from the listener thread; callers marshal to the main loop themselves.
    """

    def __init__(self, instance: Instance, on_event, on_state=None):
        self.instance = instance
        self.on_event = on_event
        self.on_state = on_state or (lambda connected: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.connected = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kodi-notify", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _set_connected(self, value: bool):
        if self.connected != value:
            self.connected = value
            try:
                self.on_state(value)
            except Exception:
                pass

    def _run(self):
        backoff = _Backoff()
        decoder = json.JSONDecoder()
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.instance.host, self.instance.tcp_port), timeout=10)
                sock.settimeout(60)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self._sock = sock
                self._set_connected(True)
                backoff.reset()
                buf = ""
                last_rx = time.monotonic()
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        # idle; send a ping to detect half-dead connections
                        if time.monotonic() - last_rx > 55:
                            sock.sendall(b'{"jsonrpc":"2.0","id":0,"method":"JSONRPC.Ping"}')
                        continue
                    if not chunk:
                        break
                    last_rx = time.monotonic()
                    buf += chunk.decode("utf-8", errors="replace")
                    while buf:
                        buf = buf.lstrip()
                        if not buf:
                            break
                        try:
                            obj, end = decoder.raw_decode(buf)
                        except ValueError:
                            break  # incomplete object; wait for more data
                        buf = buf[end:]
                        if isinstance(obj, dict) and "method" in obj and "id" not in obj:
                            try:
                                self.on_event(obj["method"], obj.get("params", {}))
                            except Exception:
                                pass
            except OSError:
                pass
            finally:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
                self._set_connected(False)
            if not self._stop.is_set():
                self._stop.wait(backoff.next())
