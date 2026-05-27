"""
Tiny single-file server for the Claude Feedback library.

Serves a directory of HTML artifacts AND accepts comment-batch submissions from
the in-page library. Submissions are appended to <artifact>/feedback/inbox.jsonl
where Claude (the agent) can pick them up, process them, and append to
<artifact>/feedback/history.json. The page polls history.json to detect new
changes and offer a walkthrough.

Usage:
    python lib/server.py <artifact_dir> [--port 5050]

There are NO dependencies beyond the Python standard library.
"""
import argparse
import http.server
import json
import mimetypes
import os
import posixpath
import socketserver
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

# Project-root lib directory (where this file lives). The server serves
# /lib/<file> from here so artifacts can <script src="/lib/feedback.js">
# instead of inlining — library updates apply on a simple page refresh.
LIB_DIR = Path(__file__).resolve().parent

# Cap incoming POST bodies. Comments are small JSON; this is a generous
# ceiling that still bounds memory/disk consumption per request.
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# history.json is the only file in <artifact>/feedback/ that the in-page
# library needs to fetch (it polls for new walkthroughs). Everything else —
# inbox.jsonl, lastseen.json, and any future bookkeeping — is agent-side
# state that browser/HTTP clients should not be able to read.
FEEDBACK_PUBLIC_FILES = frozenset({"/feedback/history.json"})

# ---------- Auto-shutdown bookkeeping ----------
# Servers launched as Claude Code background tasks would otherwise outlive the
# session (orphaned to launchd/init) and accumulate. Two complementary checks:
#   1. parent-death — if our parent process exits, we get reparented to PID 1.
#      Skip this watchdog if we were already detached at startup (e.g. nohup).
#   2. idle timeout — the page polls every ~4s, so any live browser keeps us
#      alive. When no requests have arrived for IDLE_TIMEOUT_S, exit.
INITIAL_PPID = os.getppid()
_activity_lock = threading.Lock()
_last_activity = time.monotonic()


def _touch_activity():
    global _last_activity
    with _activity_lock:
        _last_activity = time.monotonic()


def _idle_seconds():
    with _activity_lock:
        return time.monotonic() - _last_activity


def _with_charset(content_type: str) -> str:
    """Append `; charset=utf-8` to text-ish content types when missing. Without
    this, browsers fall back to Latin-1 and emojis / non-ASCII glyphs garble."""
    if not content_type:
        return content_type
    needs = (
        content_type.startswith("text/")
        or content_type in ("application/javascript", "application/json", "application/xml")
    )
    if needs and "charset=" not in content_type.lower():
        return f"{content_type}; charset=utf-8"
    return content_type


class FeedbackHandler(http.server.SimpleHTTPRequestHandler):
    feedback_dir: Path = None  # type: ignore
    artifact_dir: Path = None  # type: ignore

    # ---------- override caching: dev server should never cache ----------
    def end_headers(self):
        # Any response is proof of a live client — push the idle deadline back.
        _touch_activity()
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _check_same_origin(self) -> bool:
        # Block cross-origin POSTs from a malicious tab in the user's browser.
        # Browsers attach Origin on cross-origin POSTs; same-origin fetches
        # from the in-page library, curl, and the agent typically do not.
        # If Origin is set, it must name this server's host.
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    def _read_body(self):
        # Read the request body with a hard size cap. On error, sends a
        # response and returns None so the caller can just bail.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid content-length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "payload too large"})
            return None
        return self.rfile.read(length).decode("utf-8") if length else ""

    def guess_type(self, path):
        # SimpleHTTPRequestHandler uses this to set Content-Type. Force UTF-8
        # for text/*, JS, JSON so emojis and non-ASCII glyphs render correctly.
        return _with_charset(super().guess_type(path))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/info":
            # Diagnostic endpoint: lets other Claude Code sessions detect what
            # this server is serving so they know whether to reuse or take over.
            info = {
                "artifact_dir": str(self.artifact_dir),
                "feedback_dir": str(self.feedback_dir),
                "lib_dir": str(LIB_DIR),
                "port": self.server.server_address[1],
            }
            self._json(200, info)
            return
        if parsed.path.startswith("/lib/"):
            self._serve_from_lib(parsed.path[len("/lib/"):])
            return
        # Block HTTP reads of anything under /feedback/ except history.json.
        # Normalize first so dot-segments (/./feedback/inbox.jsonl), double
        # slashes, and percent-encoding (/%66eedback/...) all collapse to
        # the canonical form. Lowercase too — macOS APFS and Windows NTFS
        # are case-insensitive, so /FEEDBACK/inbox.jsonl would otherwise
        # bypass the prefix check and translate_path would still find the
        # file. The bare "/feedback" / "/feedback/" cases are blocked
        # explicitly to prevent SimpleHTTPRequestHandler from rendering
        # a directory listing that leaks the inbox/lastseen filenames.
        safe_path = posixpath.normpath(unquote(parsed.path)).lower()
        if safe_path == "/feedback" or (
            safe_path.startswith("/feedback/") and safe_path not in FEEDBACK_PUBLIC_FILES
        ):
            self.send_error(403, "forbidden")
            return
        super().do_GET()

    def _serve_from_lib(self, rel: str):
        # Path-traversal-safe lookup inside LIB_DIR
        try:
            target = (LIB_DIR / rel).resolve()
        except Exception:
            self.send_error(404); return
        if not str(target).startswith(str(LIB_DIR) + os.sep) and target != LIB_DIR:
            self.send_error(403, "forbidden"); return
        if not target.exists() or not target.is_file():
            self.send_error(404); return
        mime, _ = mimetypes.guess_type(str(target))
        if mime is None:
            mime = "application/octet-stream"
        mime = _with_charset(mime)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._check_same_origin():
            self._json(403, {"ok": False, "error": "cross-origin POST rejected"})
            return

        if parsed.path == "/feedback":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "error": "expected json object"})
                return
            data["received_at"] = time.time()
            data["received_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            inbox = self.feedback_dir / "inbox.jsonl"
            with open(inbox, "a") as f:
                f.write(json.dumps(data) + "\n")
            sys.stdout.write(f"[feedback] batch with {len(data.get('comments', []))} comment(s) -> {inbox}\n")
            sys.stdout.flush()
            self._json(200, {"ok": True})
            return

        if parsed.path == "/mark-seen":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "error": "expected json object"})
                return
            seen_path = self.feedback_dir / "lastseen.json"
            seen_path.write_text(json.dumps(data, indent=2))
            self._json(200, {"ok": True})
            return

        self._json(404, {"ok": False, "error": "unknown endpoint"})

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Silence the default request logging — too noisy for our purposes.
    def log_message(self, format, *args):
        # Only log POSTs and errors. args[0] is typically the request line
        # (str) from log_request, but log_error passes the response code as
        # an int — stringify before testing.
        if not args:
            return
        first = str(args[0])
        joined = " ".join(str(a) for a in args)
        if first.startswith("POST") or " 4" in joined or " 5" in joined:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def _watchdog(idle_timeout_s: int):
    """Daemon thread: terminate the server when (a) the parent process dies,
    or (b) no client has hit us for idle_timeout_s. Polls every 5s. Uses
    os._exit because srv.shutdown() can hang on the per-request thread join
    that ThreadingTCPServer.server_close() does by default — and for a dev
    server graceful close has no upside."""
    watch_parent = (INITIAL_PPID != 1)
    while True:
        time.sleep(5)
        reason = None
        if watch_parent and os.getppid() == 1:
            reason = "parent process exited"
        elif idle_timeout_s > 0 and _idle_seconds() > idle_timeout_s:
            reason = f"idle for >{idle_timeout_s}s with no clients"
        if reason:
            sys.stdout.write(f"[server] {reason}; shutting down\n")
            sys.stdout.flush()
            os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact_dir", help="directory containing the HTML artifact")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="interface to bind to. Default 127.0.0.1 (loopback only). "
                         "Use 0.0.0.0 to expose on the LAN — only do this on a trusted network.")
    ap.add_argument("--idle-timeout", type=int, default=600,
                    help="exit if no client requests for this many seconds (0 = disable). Default 600 (10 min).")
    args = ap.parse_args()

    artifact_dir = Path(args.artifact_dir).resolve()
    if not artifact_dir.exists():
        print(f"ERROR: {artifact_dir} does not exist")
        sys.exit(1)

    feedback_dir = artifact_dir / "feedback"
    feedback_dir.mkdir(exist_ok=True)
    inbox = feedback_dir / "inbox.jsonl"
    if not inbox.exists():
        inbox.touch()
    history = feedback_dir / "history.json"
    if not history.exists():
        history.write_text("[]")

    FeedbackHandler.feedback_dir = feedback_dir
    FeedbackHandler.artifact_dir = artifact_dir

    os.chdir(artifact_dir)

    # socketserver.TCPServer doesn't reuse the port quickly enough — subclass:
    class ReuseTCP(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        # Per-request handler threads are daemon so they don't block process
        # exit if a client connection lingers.
        daemon_threads = True

    try:
        srv = ReuseTCP((args.bind, args.port), FeedbackHandler)
    except OSError as e:
        print(f"[server] FATAL: {args.bind}:{args.port} is unavailable ({e}).")
        print(f"[server]  - check what's running there:  curl -s http://localhost:{args.port}/info")
        print(f"[server]  - or kill it:                  lsof -ti:{args.port} | xargs kill")
        print(f"[server]  - or run me on a different port: --port {args.port + 1}")
        sys.exit(1)

    # Auto-shutdown so servers don't accumulate across Claude Code sessions.
    threading.Thread(
        target=_watchdog, args=(args.idle_timeout,), daemon=True
    ).start()

    with srv:
        print(f"[server] serving {artifact_dir} on {args.bind}:{args.port}")
        if args.bind not in ("127.0.0.1", "localhost"):
            print(f"[server] WARNING: bound to {args.bind} — reachable beyond loopback")
        print(f"[server] open http://localhost:{args.port}/sample.html")
        print(f"[server] inbox:   {inbox}")
        print(f"[server] history: {history}")
        print(f"[server] info:    http://localhost:{args.port}/info")
        if args.idle_timeout > 0:
            print(f"[server] auto-shutdown: parent-death OR {args.idle_timeout}s idle (no requests). --idle-timeout 0 to disable")
        else:
            print(f"[server] auto-shutdown: parent-death only (idle timeout disabled)")
        print(f"[server] Ctrl-C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[server] stopping")


if __name__ == "__main__":
    main()
