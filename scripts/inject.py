"""
Inject (or remove) the Claude Feedback library tags in every *.html file in a
directory. Also creates feedback/inbox.jsonl and feedback/history.json so the
server has somewhere to write.

Idempotent — running twice is a no-op. Files that already have the tags are
left untouched. Files that have no </head> or </body> are reported and skipped.

Usage:
    python inject.py <dir> [--remove] [--recursive]
"""
import argparse
import re
import sys
from pathlib import Path

CSS_TAG = '<link rel="stylesheet" href="/lib/feedback.css">'
JS_TAG = '<script src="/lib/feedback.js" defer></script>'

CSS_MARKER = "/lib/feedback.css"
JS_MARKER = "/lib/feedback.js"

CSS_REMOVE_RE = re.compile(
    r'[ \t]*<link[^>]*href=["\']/lib/feedback\.css["\'][^>]*>\s*\n?',
    re.IGNORECASE,
)
JS_REMOVE_RE = re.compile(
    r'[ \t]*<script[^>]*src=["\']/lib/feedback\.js["\'][^>]*></script>\s*\n?',
    re.IGNORECASE,
)


def inject_one(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    changed = False
    notes = []
    css_present = CSS_MARKER in text
    js_present = JS_MARKER in text

    if not css_present:
        if "</head>" in text:
            text = text.replace("</head>", f"  {CSS_TAG}\n</head>", 1)
            changed = True
        else:
            notes.append("no </head>")
    if not js_present:
        if "</body>" in text:
            text = text.replace("</body>", f"  {JS_TAG}\n</body>", 1)
            changed = True
        else:
            notes.append("no </body>")

    if changed:
        path.write_text(text, encoding="utf-8")
        status = "injected"
    elif css_present and js_present:
        status = "skipped (already wired)"
    else:
        status = "skipped (cannot wire)"
    if notes:
        status += " [" + ", ".join(notes) + "]"
    return status


def remove_one(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    new_text, n_css = CSS_REMOVE_RE.subn("", text)
    new_text, n_js = JS_REMOVE_RE.subn("", new_text)
    if n_css + n_js == 0:
        return "skipped (no tags)"
    path.write_text(new_text, encoding="utf-8")
    return f"removed ({n_css} css, {n_js} js)"


def find_html(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return sorted(p for p in root.rglob("*.html") if "feedback" not in p.parts)
    return sorted(root.glob("*.html"))


def ensure_feedback_dir(root: Path) -> None:
    fb = root / "feedback"
    fb.mkdir(exist_ok=True)
    inbox = fb / "inbox.jsonl"
    if not inbox.exists():
        inbox.touch()
    history = fb / "history.json"
    if not history.exists():
        history.write_text("[]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir", help="directory containing HTML files")
    ap.add_argument("--remove", action="store_true", help="strip the tags instead of injecting")
    ap.add_argument("--recursive", "-r", action="store_true", help="walk subdirectories")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 1

    htmls = find_html(root, args.recursive)
    if not htmls:
        print(f"No *.html files found in {root}")
        return 0

    action = remove_one if args.remove else inject_one
    verb = "Removing tags from" if args.remove else "Injecting tags into"
    print(f"{verb} {len(htmls)} file(s) under {root}:")
    for p in htmls:
        rel = p.relative_to(root)
        print(f"  {rel}: {action(p)}")

    if not args.remove:
        ensure_feedback_dir(root)
        print(f"\nFeedback dir ready: {root / 'feedback'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
