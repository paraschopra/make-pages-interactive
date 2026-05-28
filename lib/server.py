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
import re
import socketserver
import sys
import threading
import time
import uuid
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse

# Project-root lib directory (where this file lives). The server serves
# /lib/<file> from here so artifacts can <script src="/lib/feedback.js">
# instead of inlining — library updates apply on a simple page refresh.
LIB_DIR = Path(__file__).resolve().parent

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


class EditError(Exception):
    pass


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _find_element_block(text: str, *, cf_id: str = None, element_id: str = None):
    """Locate an element by data-cf-id or id and return (inner_start, inner_end)
    indices spanning its inner HTML. Naive regex-based scan — fine for the
    structured HTML this skill targets. Returns None if not found or if the
    element is self-closing / void."""
    if cf_id:
        attr_re = re.compile(r'data-cf-id\s*=\s*"' + re.escape(cf_id) + r'"')
    elif element_id:
        attr_re = re.compile(r'\bid\s*=\s*"' + re.escape(element_id) + r'"')
    else:
        return None
    m = attr_re.search(text)
    if not m:
        return None
    # Walk back to find the opening '<', then find the tag name.
    open_lt = text.rfind("<", 0, m.start())
    if open_lt < 0:
        return None
    tag_match = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)\b", text[open_lt:])
    if not tag_match:
        return None
    tag_name = tag_match.group(1).lower()
    if tag_name in ("img", "br", "hr", "input", "meta", "link", "source", "track", "wbr", "area", "base", "col", "embed", "param"):
        return None
    # Find the end of the opening tag.
    open_gt = text.find(">", m.end())
    if open_gt < 0:
        return None
    # Find the matching closing tag, allowing nested same-tag pairs.
    inner_start = open_gt + 1
    depth = 1
    pos = inner_start
    pair_re = re.compile(r"<(/?)" + re.escape(tag_name) + r"\b", re.IGNORECASE)
    while depth > 0:
        nxt = pair_re.search(text, pos)
        if not nxt:
            return None
        if nxt.group(1) == "/":
            depth -= 1
            if depth == 0:
                return (inner_start, nxt.start())
        else:
            # Skip the rest of this opening tag (could be self-closing inside).
            gt = text.find(">", nxt.end())
            if gt < 0:
                return None
            # Ignore self-closing form (e.g. <p />) — extremely rare for these tags.
            if text[gt - 1] != "/":
                depth += 1
            pos = gt + 1
            continue
        pos = nxt.end()
    return None


def _visible_ranges(text: str, start: int, end: int):
    """Yield (a, b) ranges of source within [start, end) that contain VISIBLE
    text — i.e. outside of HTML tags, comments, and <script>/<style> blocks.
    Used to skip over markup when searching for plain-text occurrences."""
    i = start
    n = end
    lower = text  # we'll compare lowercased only at the script/style boundary
    while i < n:
        # Skip HTML comments.
        if text.startswith("<!--", i):
            close = text.find("-->", i + 4)
            i = (close + 3) if close >= 0 else n
            continue
        # Skip <script>…</script> and <style>…</style> (case-insensitive).
        if text.startswith("<", i):
            # Check for script/style.
            for blk in ("script", "style"):
                if lower[i + 1:i + 1 + len(blk)].lower() == blk and lower[i + 1 + len(blk):i + 1 + len(blk) + 1] in (" ", ">", "\t", "\n", "/"):
                    # Find end of opening tag.
                    gt = text.find(">", i)
                    if gt < 0:
                        i = n
                        break
                    # Find matching closing tag.
                    end_tag = f"</{blk}"
                    close = text.lower().find(end_tag, gt + 1)
                    if close < 0:
                        i = n
                        break
                    # Skip past </blk ... >
                    close_gt = text.find(">", close)
                    i = (close_gt + 1) if close_gt >= 0 else n
                    break
            else:
                # A normal tag — find its '>'.
                gt = text.find(">", i)
                i = (gt + 1) if gt >= 0 else n
                continue
            continue
        # Otherwise we're at a visible-text run. Find the next '<' or end.
        nxt = text.find("<", i)
        if nxt < 0 or nxt >= n:
            yield (i, n)
            i = n
        else:
            yield (i, nxt)
            i = nxt


def _find_visible_text_occurrences(text: str, start: int, end: int, needle_decoded: str, needle_encoded: str):
    """Return ordered list of (match_start, match_end) source-offset pairs
    where the needle appears in the page's visible text — even if the match
    straddles inline tags like <strong>…</strong>.

    Strategy: flatten all visible-text ranges into a single string, with a
    parallel position map back to source offsets. Search the flattened
    string. For each hit, the source range spans from the first character's
    source offset to the last character's source offset + 1, which naturally
    includes any inline tags caught between them."""
    ranges = list(_visible_ranges(text, start, end))
    # flat[k] is the visible character; offsets[k] is its position in `text`.
    flat_chars = []
    offsets = []
    for (a, b) in ranges:
        for k in range(a, b):
            flat_chars.append(text[k])
            offsets.append(k)
    flat = "".join(flat_chars)
    # Try encoded first, then decoded.
    for needle in (needle_encoded, needle_decoded):
        if not needle:
            continue
        hits = []
        i = 0
        while True:
            j = flat.find(needle, i)
            if j < 0:
                break
            src_start = offsets[j]
            src_end = offsets[j + len(needle) - 1] + 1
            hits.append((src_start, src_end))
            i = j + len(needle)
        if hits:
            return hits
    return []


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
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
        if parsed.path == "/feedback":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
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

        if parsed.path == "/edit":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                result = self._apply_edit(data)
            except EditError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            self._json(200, {"ok": True, **result})
            return

        if parsed.path == "/mark-seen":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
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

    # ---------- direct-edit support ----------
    def _apply_edit(self, data: dict):
        """Apply a plain-text direct edit to an HTML file. Schema:
            { page_url, cf_id?, element_id?, old_text, new_text, comment? }
        Locates the element by id or data-cf-id, replaces the first occurrence
        of old_text inside that element's inner HTML, wraps the replacement in
        <span data-cf-change="ch-<slug>">…</span>, appends a batch to
        history.json, and logs an entry to inbox.jsonl. Plain text only — if
        old_text contains '<' or '>' or doesn't appear exactly once inside the
        element, the edit is rejected."""
        page_url = (data.get("page_url") or "").lstrip("/")
        old_text = data.get("old_text") or ""
        new_text = data.get("new_text") if data.get("new_text") is not None else ""
        cf_id = data.get("cf_id")
        element_id = data.get("element_id")
        comment = (data.get("comment") or "").strip()
        # occurrence_index: when the same `old_text` appears multiple times,
        # this picks which one to edit. 0 = first, 1 = second, etc. Computed
        # client-side by counting visible-text occurrences before the edit.
        try:
            occurrence_index = int(data.get("occurrence_index", 0))
        except (TypeError, ValueError):
            occurrence_index = 0
        if occurrence_index < 0:
            occurrence_index = 0

        if not page_url:
            raise EditError("missing page_url")
        if not old_text:
            raise EditError("missing old_text")
        if "<" in old_text or ">" in old_text or "<" in new_text or ">" in new_text:
            raise EditError("plain text only — angle brackets not allowed")
        if old_text == new_text:
            raise EditError("no change")
        # cf_id / element_id are optional — if neither is present (or neither
        # is found in the file, since cf_id is assigned by the client at
        # runtime and won't be in the HTML), we fall back to text-anchored
        # matching: find old_text in the file directly, require exactly one
        # match, wrap in place.

        # Path-traversal-safe resolution within artifact_dir
        candidate = (self.artifact_dir / page_url).resolve()
        if not str(candidate).startswith(str(self.artifact_dir) + os.sep) and candidate != self.artifact_dir:
            raise EditError("page_url escapes artifact dir")
        if not candidate.exists() or candidate.suffix.lower() != ".html":
            raise EditError(f"page not found: {page_url}")

        text = candidate.read_text(encoding="utf-8")

        # 1) Try to scope to the targeted element if we have an anchor that's
        #    actually in the file (id only; cf_id is runtime).
        match = None
        if element_id:
            match = _find_element_block(text, element_id=element_id)
        elif cf_id:
            # cf_id is runtime-only — won't be in the file. Skip.
            match = _find_element_block(text, cf_id=cf_id)

        change_id = "ch-edit-" + uuid.uuid4().hex[:8]
        # The browser sends decoded text (DOM textContent). The HTML file
        # stores it encoded (&amp;, &quot;, etc.). Encode before searching.
        old_encoded = html_escape(old_text, quote=False)
        new_encoded = html_escape(new_text, quote=False)
        wrapped = f'<span data-cf-change="{change_id}">{new_encoded}</span>'

        def _locate(haystack: str, needle: str) -> int:
            """Try the encoded needle first, then decoded fallback. Returns -1
            if not found. We tolerate either form because some authors hand-
            write entities (&amp;) while others rely on UTF-8 (&)."""
            n = haystack.count(needle)
            if n > 0:
                return n
            return haystack.count(old_text) if needle != old_text else 0

        # Whether we scoped by element or fall back to body-wide, the
        # disambiguation strategy is the same: find all occurrences of
        # old_text within VISIBLE text only (skipping tags, scripts, styles,
        # comments), and pick the one at occurrence_index. The client computed
        # that index by walking the live DOM in the same document order.
        if match:
            block_start, block_end = match
            search_start, search_end = block_start, block_end
            scope_label = "targeted element"
        else:
            body_open = text.lower().find("<body")
            body_close = text.lower().rfind("</body>")
            if body_open < 0 or body_close < 0:
                raise EditError("could not locate <body> in HTML")
            search_start = text.find(">", body_open) + 1
            search_end = body_close
            scope_label = "page"

        hits = _find_visible_text_occurrences(text, search_start, search_end, old_text, old_encoded)
        if not hits:
            raise EditError(f"element not found in HTML (no occurrences in {scope_label})")
        if occurrence_index >= len(hits):
            raise EditError(
                f"occurrence_index {occurrence_index} out of range — only {len(hits)} matches in {scope_label}"
            )
        hit_start, hit_end = hits[occurrence_index]
        candidate.write_text(text[:hit_start] + wrapped + text[hit_end:], encoding="utf-8")

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        history_path = self.feedback_dir / "history.json"
        try:
            existing = json.loads(history_path.read_text(encoding="utf-8") or "[]")
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, FileNotFoundError):
            existing = []
        batch = {
            "batch_id": "b-edit-" + uuid.uuid4().hex[:8],
            "timestamp": now_iso,
            "source": "direct-edit",
            "comments": [],
            "changes": [
                {
                    "id": change_id,
                    "anchor": change_id,
                    "in_response_to": [],
                    "title": f"edited: \"{_truncate(old_text, 40)}\" → \"{_truncate(new_text, 40)}\"",
                    "description": comment or f"Direct edit in {page_url}",
                    "kind": "edit",
                    "page_url": page_url,
                    "old_text": old_text,
                    "new_text": new_text,
                    "selector_hint": cf_id or element_id,
                }
            ],
        }
        existing.append(batch)
        history_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        inbox_entry = {
            "kind": "edit-log",
            "received_at": time.time(),
            "received_iso": now_iso,
            "page_url": page_url,
            "change_id": change_id,
            "old_text": old_text,
            "new_text": new_text,
            "selector_hint": cf_id or element_id,
            "comment": comment,
        }
        with open(self.feedback_dir / "inbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(inbox_entry) + "\n")

        sys.stdout.write(f"[edit] {page_url}: {_truncate(old_text, 40)!r} -> {_truncate(new_text, 40)!r} ({change_id})\n")
        sys.stdout.flush()
        return {"change_id": change_id, "batch_id": batch["batch_id"]}

    # Silence the default request logging — too noisy for our purposes.
    def log_message(self, format, *args):
        # Only log POSTs and errors
        if args and (args[0].startswith("POST") or " 4" in " ".join(map(str, args)) or " 5" in " ".join(map(str, args))):
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
        srv = ReuseTCP(("", args.port), FeedbackHandler)
    except OSError as e:
        print(f"[server] FATAL: port {args.port} is unavailable ({e}).")
        print(f"[server]  - check what's running there:  curl -s http://localhost:{args.port}/info")
        print(f"[server]  - or kill it:                  lsof -ti:{args.port} | xargs kill")
        print(f"[server]  - or run me on a different port: --port {args.port + 1}")
        sys.exit(1)

    # Auto-shutdown so servers don't accumulate across Claude Code sessions.
    threading.Thread(
        target=_watchdog, args=(args.idle_timeout,), daemon=True
    ).start()

    with srv:
        print(f"[server] serving {artifact_dir}")
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
