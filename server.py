"""
Local HTTP server for PSC Stream.

Serves the web UI from web/ and a JSON API under /api/*:

  GET  /api/status              -> {authorized, channel, total_videos, scan, mock}
  POST /api/auth/start          -> {next: 'code'|'done'}   (api_id, api_hash, phone)
  POST /api/auth/code           -> {next: 'password'|'done'} (code)
  POST /api/auth/password       -> {next: 'done'}            (password)
  POST /api/auth/logout         -> clears session + settings
  POST /api/settings/channel   -> {title}                   (channel)
  POST /api/rescan              -> starts background rescan
  GET  /api/videos?page=N       -> {videos, page, has_more, total}
  GET  /api/stream/<msg_id>     -> video bytes, full HTTP Range support (206)
  GET  /api/thumb/<msg_id>      -> thumbnail image (disk-cached) or SVG placeholder

Video bytes are NEVER written to disk: the /api/stream path keeps everything
in a small in-memory LRU chunk cache (128 MB cap). Only tiny thumbnails are
cached on disk (app_data/thumbs/).

Stdlib only (ThreadingHTTPServer) so PyInstaller packaging stays trivial.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import tg_bridge
from tg_bridge import BridgeError

PAGE_SIZE = 60
RESCAN_PAGE = 100
RESCAN_CAP = 3000  # max videos per rescan, safety bound
BLOCK_SIZE = tg_bridge.BLOCK_SIZE  # 512 KiB, matches telethon fetch blocks
CACHE_MAX_BYTES = 128 * 1024 * 1024  # in-memory video chunk cache cap
WRITE_CHUNK = 1024 * 1024  # how much we hand to the socket per write

MOCK = os.environ.get("PSC_STREAM_MOCK") == "1"


def find_vlc():
    """Return the path to vlc(.exe), or None if VLC is not installed.

    Checked: PATH, the standard Windows install locations, and the
    uninstall registry key. Only these trusted locations are ever launched.
    """
    found = shutil.which("vlc") or shutil.which("vlc.exe")
    if found:
        return found
    if os.name == "nt":
        progfiles = os.environ.get("ProgramFiles", r"C:\Program Files")
        progfiles_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in (progfiles, progfiles_x86):
            cand = os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
            if os.path.isfile(cand):
                return cand
        try:
            import winreg
            for root, sub in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\VideoLAN\VLC"),
            ):
                try:
                    with winreg.OpenKey(root, sub) as key:
                        d, _ = winreg.QueryValueEx(key, "InstallDir")
                    cand = os.path.join(d, "vlc.exe")
                    if os.path.isfile(cand):
                        return cand
                except OSError:
                    continue
        except ImportError:
            pass
    return None


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def resource_path(rel):
    """Resolve a bundled file both in dev and inside a PyInstaller onefile exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def app_data_dir():
    """Writable per-user dir for session, settings, index cache, thumbnails."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "PSCStream")
    else:
        d = os.path.join(os.path.expanduser("~"), ".psc-stream")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# In-memory LRU chunk cache (video bytes only, never on disk)
# ---------------------------------------------------------------------------

class ChunkCache:
    def __init__(self, block_size=BLOCK_SIZE, max_bytes=CACHE_MAX_BYTES):
        self.block_size = block_size
        self.max_bytes = max_bytes
        self._data = OrderedDict()  # (msg_id, block_idx) -> bytes
        self._size = 0
        self._lock = threading.Lock()

    def _get_block(self, msg_id, block_idx, fetch):
        key = (msg_id, block_idx)
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        # fetch(msg_id, start, end_inclusive) -> bytes; may return short at EOF
        data = fetch(msg_id, block_idx * self.block_size,
                     (block_idx + 1) * self.block_size - 1)
        with self._lock:
            self._data[key] = data
            self._size += len(data)
            while self._size > self.max_bytes and self._data:
                _k, old = self._data.popitem(last=False)
                self._size -= len(old)
        return data

    def get_range(self, msg_id, start, end, fetch):
        """Assemble exact bytes for inclusive [start, end] from cached blocks."""
        out = bytearray()
        bs = self.block_size
        for b in range(start // bs, end // bs + 1):
            block = self._get_block(msg_id, b, fetch)
            lo = max(start, b * bs) - b * bs
            hi = min(end, b * bs + len(block) - 1) - b * bs
            if hi >= lo and lo < len(block):
                out += block[lo:hi + 1]
        return bytes(out)

    def stats(self):
        with self._lock:
            return {"blocks": len(self._data), "bytes": self._size}


# ---------------------------------------------------------------------------
# Range parsing
# ---------------------------------------------------------------------------

def parse_range(header_value, size):
    """
    Parse a `Range: bytes=...` header. Returns (start, end) inclusive.
    Raises ValueError if the header is malformed or unsatisfiable.
    """
    if not header_value or not header_value.startswith("bytes="):
        raise ValueError("bad range unit")
    spec = header_value[6:].strip()
    if "," in spec:
        raise ValueError("multipart ranges not supported")
    if spec.startswith("-"):  # suffix range: last N bytes
        suffix = int(spec[1:])
        if suffix <= 0:
            raise ValueError("bad suffix")
        start, end = max(size - suffix, 0), size - 1
    else:
        a, b = spec.split("-", 1)
        start = int(a)
        end = int(b) if b.strip() else size - 1
    if start >= size or end < start or start < 0:
        raise ValueError("unsatisfiable")
    return start, min(end, size - 1)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

SVG_PLACEHOLDER = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">\
<rect width="320" height="180" fill="#161b26"/>\
<polygon points="140,60 140,120 200,90" fill="#e50914"/>\
</svg>""".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PSCStream/1.0"

    # -- helpers ---------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message, status=400, extra=None):
        obj = {"ok": False, "error": code, "message": message}
        if extra:
            obj.update(extra)
        self._json(obj, status=status)

    def _bridge_err(self, exc):
        if not isinstance(exc, BridgeError):
            self._err("unknown", "%s: %s" % (type(exc).__name__, exc), status=500)
            return
        status = {"flood_wait": 429, "not_authorized": 401, "not_found": 404}.get(
            exc.code, 400
        )
        self._err(exc.code, exc.message, status=status, extra=exc.extra or None)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/status":
                return self.api_status()
            if path == "/api/videos":
                return self.api_videos(parse_qs(parsed.query))
            m = re.fullmatch(r"/api/stream/(\d+)", path)
            if m:
                return self.api_stream(int(m.group(1)))
            m = re.fullmatch(r"/api/thumb/(\d+)", path)
            if m:
                return self.api_thumb(int(m.group(1)))
            return self.serve_static(path)
        except BridgeError as exc:
            self._bridge_err(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never leak a stack trace to the UI
            self._err("unknown", "%s: %s" % (type(exc).__name__, exc), status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/auth/start":
                return self.api_auth_start()
            if path == "/api/auth/code":
                return self.api_auth_code()
            if path == "/api/auth/password":
                return self.api_auth_password()
            if path == "/api/auth/logout":
                return self.api_auth_logout()
            if path == "/api/settings/channel":
                return self.api_set_channel()
            if path == "/api/rescan":
                return self.api_rescan()
            if path == "/api/open-vlc":
                return self.api_open_vlc()
            return self._err("not_found", "Unknown endpoint", status=404)
        except BridgeError as exc:
            self._bridge_err(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._err("unknown", "%s: %s" % (type(exc).__name__, exc), status=500)

    # -- API ---------------------------------------------------------------
    def api_status(self):
        srv = self.server
        try:
            authorized = srv.bridge.is_authorized()
        except BridgeError:
            authorized = False
        self._json(
            {
                "ok": True,
                "mock": MOCK,
                "authorized": authorized,
                "channel": srv.settings.get("channel"),
                "total_videos": len(srv.video_index),
                "scan": srv.scan_state,
                "cache": srv.chunk_cache.stats(),
            }
        )

    def api_auth_start(self):
        body = self._read_json()
        if body is None:
            return self._err("bad_request", "Invalid JSON body.")
        try:
            api_id = int(body.get("api_id") or 0)
        except (TypeError, ValueError):
            api_id = 0
        api_hash = (body.get("api_hash") or "").strip()
        phone = (body.get("phone") or "").strip().replace(" ", "")
        if not api_id or not api_hash:
            return self._err(
                "bad_request",
                "api_id and api_hash are required (get them at my.telegram.org).",
            )
        if not phone:
            return self._err("phone_invalid", "Enter your phone number.")
        try:
            result = self.server.bridge.auth_start(api_id, api_hash, phone)
        except BridgeError as exc:
            return self._bridge_err(exc)
        # Persist creds so the session auto-connects on next launch.
        self.server.settings.update(
            {"api_id": api_id, "api_hash": api_hash}
        )
        self.server.save_settings()
        self._json({"ok": True, "next": result["next"]})

    def api_auth_code(self):
        body = self._read_json()
        if body is None:
            return self._err("bad_request", "Invalid JSON body.")
        code = (body.get("code") or "").strip()
        if not code:
            return self._err("code_invalid", "Enter the code Telegram sent you.")
        try:
            result = self.server.bridge.auth_code(code)
        except BridgeError as exc:
            return self._bridge_err(exc)
        self._json({"ok": True, "next": result["next"]})

    def api_auth_password(self):
        body = self._read_json()
        if body is None:
            return self._err("bad_request", "Invalid JSON body.")
        password = body.get("password") or ""
        if not password:
            return self._err("password_invalid", "Enter your 2FA password.")
        try:
            result = self.server.bridge.auth_password(password)
        except BridgeError as exc:
            return self._bridge_err(exc)
        self._json({"ok": True, "next": result["next"]})

    def api_auth_logout(self):
        srv = self.server
        try:
            srv.bridge.logout()
        except BridgeError:
            pass
        # Delete the local session files so the next launch starts clean.
        for suffix in (".session", ".session-journal"):
            try:
                os.remove(srv.session_path + suffix)
            except OSError:
                pass
        srv.settings.pop("api_id", None)
        srv.settings.pop("api_hash", None)
        srv.settings.pop("channel", None)
        srv.save_settings()
        srv.video_index = []
        srv.save_index()
        self._json({"ok": True})

    def api_set_channel(self):
        body = self._read_json()
        if body is None:
            return self._err("bad_request", "Invalid JSON body.")
        channel = (body.get("channel") or "").strip()
        if not channel:
            return self._err("channel_not_found", "Enter a channel username or link.")
        try:
            result = self.server.bridge.set_channel(channel)
        except BridgeError as exc:
            return self._bridge_err(exc)
        self.server.settings["channel"] = channel
        self.server.save_settings()
        self._json({"ok": True, "title": result.get("title")})

    def api_rescan(self):
        srv = self.server
        if srv.scan_state["status"] == "running":
            return self._err("scan_running", "A scan is already running.", status=409)
        try:
            authorized = srv.bridge.is_authorized()
        except BridgeError:
            authorized = False
        if not authorized:
            return self._err("not_authorized", "Log in first.", status=401)
        if not srv.settings.get("channel"):
            return self._err("channel_not_set", "Set a channel in Settings first.")
        thread = threading.Thread(target=srv.run_rescan, daemon=True)
        thread.start()
        self._json({"ok": True, "started": True})

    def api_open_vlc(self):
        """Launch installed VLC playing this video's stream.

        Browsers can't switch audio tracks, so for subtitle/audio-track
        selection we hand playback to real VLC. The stream URL is served by
        this same server (Range-capable), so VLC can seek like normal.
        """
        body = self._read_json()
        if body is None:
            return self._err("bad_request", "Invalid JSON body.")
        try:
            msg_id = int(body.get("id"))
        except (TypeError, ValueError):
            return self._err("bad_request", "Missing video id.")
        srv = self.server
        meta = srv.find_video(msg_id)
        if meta is None:
            return self._err("not_found", "Video not found in the library.", status=404)
        vlc = find_vlc()
        if not vlc:
            return self._err(
                "vlc_not_found",
                "VLC media player is not installed on this computer.",
                status=404,
                extra={"download": "https://www.videolan.org/vlc/"},
            )
        port = srv.server_address[1]
        url = "http://127.0.0.1:%d/api/stream/%d" % (port, msg_id)
        try:
            subprocess.Popen(
                [vlc, url, "--meta-title", meta.get("title") or "PSC Stream"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            return self._err(
                "vlc_launch_failed",
                "Could not start VLC: %s" % exc,
                status=500,
            )
        self._json({"ok": True})

    def api_videos(self, query):
        try:
            page = max(int(query.get("page", ["0"])[0]), 0)
        except (ValueError, TypeError):
            page = 0
        index = self.server.video_index
        start = page * PAGE_SIZE
        chunk = index[start : start + PAGE_SIZE]
        self._json(
            {
                "ok": True,
                "videos": chunk,
                "page": page,
                "has_more": start + PAGE_SIZE < len(index),
                "total": len(index),
            }
        )

    def api_stream(self, msg_id):
        srv = self.server
        meta = srv.find_video(msg_id)
        if meta is None:
            # Not in the cached index: ask the bridge directly (also warms its
            # document cache so the range fetch below works).
            try:
                meta = srv.bridge.get_video(msg_id)
            except BridgeError as exc:
                return self._bridge_err(exc)
        size = int(meta["size"])
        if size <= 0:
            return self._err("not_found", "Video has no data.", status=404)
        mime = meta.get("mime_type") or "video/mp4"

        range_header = self.headers.get("Range")
        try:
            if range_header:
                start, end = parse_range(range_header, size)
                status = 206
            else:
                start, end = 0, size - 1
                status = 200
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")  # never let anything persist it
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        # Stream in WRITE_CHUNK slices assembled from the in-memory block cache.
        # Video bytes only ever live in RAM.
        try:
            pos = start
            while pos <= end:
                chunk_end = min(pos + WRITE_CHUNK - 1, end)
                data = srv.chunk_cache.get_range(
                    msg_id, pos, chunk_end, srv.bridge.fetch_range
                )
                if not data:
                    break
                self.wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # player navigated away / seeked; harmless
        except BridgeError:
            pass  # headers already sent; just end the stream

    def api_thumb(self, msg_id):
        srv = self.server
        path = os.path.join(srv.thumbs_dir, "%d.img" % msg_id)
        data = None
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                data = None
        if data is None:
            try:
                data = srv.bridge.get_thumb(msg_id)
            except BridgeError:
                data = None
            if data:
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except OSError:
                    pass
        if not data:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(SVG_PLACEHOLDER)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(SVG_PLACEHOLDER)
            return
        ctype = "image/jpeg"
        if data[:8].startswith(b"\x89PNG"):
            ctype = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ctype = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    # -- static web UI ------------------------------------------------------
    def serve_static(self, path):
        web_dir = resource_path("web")
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(web_dir, rel))
        # Block path traversal outside web/.
        if not (full == web_dir or full.startswith(web_dir + os.sep)):
            return self._err("not_found", "Not found", status=404)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.exists(full):
            # SPA fallback: unknown paths serve the app shell.
            full = os.path.join(web_dir, "index.html")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._err("not_found", "Not found", status=404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ---------------------------------------------------------------------------
# Server object: owns bridge, settings, index, caches
# ---------------------------------------------------------------------------

class StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)  # ephemeral port
        self.data_dir = app_data_dir()
        self.session_path = os.path.join(self.data_dir, "psc_stream_session")
        self.settings_path = os.path.join(self.data_dir, "settings.json")
        self.index_path = os.path.join(self.data_dir, "video_index.json")
        self.thumbs_dir = os.path.join(self.data_dir, "thumbs")
        os.makedirs(self.thumbs_dir, exist_ok=True)

        self.settings = self._load_json(self.settings_path, {})
        self.video_index = self._load_json(self.index_path, [])
        self.chunk_cache = ChunkCache()
        self.scan_state = {"status": "idle", "count": 0, "error": None}

        self.bridge = tg_bridge.make_bridge(MOCK, self.session_path)

        # Restore a saved login in the background (real mode only): rebuilds
        # the client from the saved api_id/api_hash so the persisted .session
        # file keeps working across restarts. No login code is ever re-sent.
        if (
            not MOCK
            and self.settings.get("api_id")
            and self.settings.get("api_hash")
        ):
            thread = threading.Thread(target=self._restore_session, daemon=True)
            thread.start()

    def _restore_session(self):
        try:
            self.bridge.restore(
                self.settings["api_id"],
                self.settings["api_hash"],
                self.settings.get("channel"),
            )
        except Exception:
            pass  # stay logged out; the wizard will handle it

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def save_settings(self):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f)
        except OSError:
            pass

    def save_index(self):
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self.video_index, f)
        except OSError:
            pass

    def find_video(self, msg_id):
        for v in self.video_index:
            if v["id"] == msg_id:
                return v
        return None

    def run_rescan(self):
        """Full channel scan in a background thread; updates scan_state."""
        self.scan_state = {"status": "running", "count": 0, "error": None}
        try:
            # Re-apply the saved channel (also validates access).
            channel = self.settings.get("channel")
            if channel:
                try:
                    self.bridge.set_channel(channel)
                except BridgeError:
                    pass
            videos = []
            offset_id = 0
            while len(videos) < RESCAN_CAP:
                page = self.bridge.scan_page(offset_id=offset_id, limit=RESCAN_PAGE)
                if not page:
                    break
                videos.extend(page)
                offset_id = page[-1]["id"]  # newest-first; continue older
                self.scan_state["count"] = len(videos)
            self.video_index = videos
            self.save_index()
            self.scan_state = {"status": "done", "count": len(videos), "error": None}
        except BridgeError as exc:
            self.scan_state = {"status": "error", "count": 0, "error": exc.message}
        except Exception as exc:
            self.scan_state = {
                "status": "error",
                "count": 0,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }


def run_server():
    server = StreamServer()
    thread = threading.Thread(
        target=server.serve_forever, name="http-server", daemon=True
    )
    thread.start()
    return server
