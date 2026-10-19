"""Tiny fake Lidarr v1 API for tests and UI development.

Run standalone:  python3 -m tests.mock_lidarr --port 8686   (API key: testkey)
"""
from __future__ import annotations

import argparse
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "testkey"

CATALOG = [
    {"foreignArtistId": "mb-1", "artistName": "Boards of Canada", "artistType": "Group",
     "disambiguation": "Scottish electronic duo", "status": "continuing",
     "overview": "Electronic music duo from Scotland.", "genres": ["Electronic", "IDM"],
     "images": [{"coverType": "poster", "url": "/MediaCover/1/poster.jpg",
                 "remoteUrl": "https://example.invalid/boc.jpg"}],
     "links": [{"url": "https://musicbrainz.org/artist/mb-1", "name": "musicbrainz"}],
     "ratings": {"votes": 10, "value": 8.7}},
    {"foreignArtistId": "mb-2", "artistName": "Boards", "artistType": "Person",
     "overview": "", "genres": [], "images": [], "links": [], "status": "ended"},
    {"foreignArtistId": "mb-3", "artistName": "The Velvet Moons", "artistType": "Group",
     "overview": "Already in Kodi under a slightly different name.", "genres": ["Rock"], "images": []},
]


class FakeLidarr:
    def __init__(self):
        self.artists: list[dict] = []
        self.commands: list[dict] = []
        self.next_id = 1

    def handle(self, method, path, query, body):
        if path == "/api/v1/system/status":
            return 200, {"appName": "Lidarr", "version": "2.9.6.4552", "instanceName": "Lidarr"}
        if path == "/api/v1/artist" and method == "GET":
            return 200, self.artists
        if path == "/api/v1/artist" and method == "POST":
            for k in ("qualityProfileId", "metadataProfileId", "rootFolderPath", "foreignArtistId"):
                if not body.get(k):
                    return 400, [{"propertyName": k, "errorMessage": f"'{k}' must not be empty."}]
            if any(a["foreignArtistId"] == body["foreignArtistId"] for a in self.artists):
                return 400, [{"errorMessage": "This artist has already been added"}]
            a = dict(body, id=self.next_id, statistics={"albumCount": 2, "trackFileCount": 0,
                                                       "totalTrackCount": 20})
            self.next_id += 1
            self.artists.append(a)
            return 201, a
        if path.startswith("/api/v1/artist/") and path[15:].isdigit():
            aid = int(path[15:])
            a = next((x for x in self.artists if x["id"] == aid), None)
            if a is None:
                return 404, {"message": "NotFound"}
            if method == "PUT":
                a.update(body)
            return 200, a
        if path == "/api/v1/artist/lookup":
            term = query.get("term", [""])[0].lower()
            if term.startswith("lidarr:"):
                res = [c for c in CATALOG if c["foreignArtistId"] == term[7:]]
            else:
                res = [c for c in CATALOG if term in c["artistName"].lower()]
            return 200, [dict(c) for c in res]
        if path == "/api/v1/album":
            aid = int(query.get("artistId", ["0"])[0])
            return 200, [{"id": 10 + aid, "title": "Music Has the Right", "releaseDate": "1998-04-20T00:00:00Z",
                          "albumType": "Album", "monitored": True,
                          "statistics": {"trackFileCount": 3, "totalTrackCount": 18}}]
        if path == "/api/v1/rootfolder":
            return 200, [{"id": 1, "path": "/music/", "freeSpace": 10**12}]
        if path == "/api/v1/qualityprofile":
            return 200, [{"id": 1, "name": "Lossless"}, {"id": 2, "name": "Standard"}]
        if path == "/api/v1/metadataprofile":
            return 200, [{"id": 1, "name": "Standard"}]
        if path == "/api/v1/command":
            self.commands.append(body)
            return 201, dict(body, id=len(self.commands), status="queued")
        if path == "/api/v1/queue":
            return 200, {"records": [{"title": "Geogaddi", "status": "downloading", "size": 100,
                                      "sizeleft": 40, "timeleft": "00:03:00",
                                      "artist": {"artistName": "Boards of Canada"},
                                      "album": {"title": "Geogaddi"}}]}
        if path == "/api/v1/wanted/missing":
            return 200, {"records": [{"title": "Tomorrow's Harvest", "releaseDate": "2013-06-05T00:00:00Z",
                                      "artist": {"artistName": "Boards of Canada"}, "monitored": True}]}
        if path == "/api/v1/calendar":
            return 200, [{"title": "New Record", "releaseDate": "2026-11-20T00:00:00Z",
                          "artist": {"artistName": "Boards of Canada"}}]
        if path.startswith("/MediaCover/"):
            return 404, {"message": "no image"}
        return 404, {"message": f"no route {path}"}


def serve(fake: FakeLidarr, port=0):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _do(self, method):
            u = urllib.parse.urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            if self.headers.get("X-Api-Key") != API_KEY:
                code, out = 401, {"error": "Unauthorized"}
            else:
                code, out = fake.handle(method, u.path, urllib.parse.parse_qs(u.query), body)
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def do_PUT(self):
            self._do("PUT")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.handle_error = lambda *a: None  # clients that time out on purpose aren't errors
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8686)
    a = ap.parse_args()
    s = serve(FakeLidarr(), a.port)
    print(f"Fake Lidarr on http://127.0.0.1:{a.port} (API key: {API_KEY})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.shutdown()
