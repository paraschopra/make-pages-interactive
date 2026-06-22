"""
render.py — markdown → GitHub-ish HTML, ready for make-pages-interactive injection.

Usage:
    python render.py <src.md> <out.html> [--title "Page title"] [--nav "a.html:Label,b.html:Label"]
    python render.py --index <out_dir>   # write an index.html linking to every *.html

Requires:
    pip3 install --user markdown

License: MIT (same as the make-pages-interactive repository).
"""
import argparse, sys
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit("pip3 install --user markdown")

BASE_CSS = """
:root { --fg:#1f2328; --muted:#57606a; --bg:#ffffff; --accent:#0969da; --border:#d0d7de; --code-bg:#f6f8fa; }
* { box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
       font-size:16px; line-height:1.6; color:var(--fg); background:var(--bg);
       max-width:820px; margin:0 auto; padding:40px 32px 120px; }
h1,h2,h3,h4 { line-height:1.25; margin-top:1.8em; }
h1 { font-size:1.9em; border-bottom:1px solid var(--border); padding-bottom:.3em; margin-top:0; }
h2 { font-size:1.4em; } h3 { font-size:1.15em; } h4 { font-size:1em; color:var(--muted); }
hr { border:0; border-top:1px solid var(--border); margin:2em 0; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
code { background:var(--code-bg); padding:.15em .4em; border-radius:4px; font-size:.92em; }
pre { background:var(--code-bg); padding:14px 16px; border-radius:6px; overflow-x:auto; font-size:.88em; }
pre code { background:transparent; padding:0; }
ul,ol { padding-left:1.6em; } li { margin:.25em 0; }
blockquote { border-left:3px solid var(--border); margin:1em 0; padding:.4em 1em;
             color:var(--muted); background:#f8fafc; }
details { background:#f6f8fa; border:1px solid var(--border); border-radius:6px;
          padding:8px 14px; margin:1em 0; }
details summary { cursor:pointer; font-weight:600; }
table { border-collapse:collapse; margin:1em 0; }
th,td { border:1px solid var(--border); padding:6px 10px; }
th { background:var(--code-bg); }
.nav { margin-bottom:2em; padding-bottom:1em; border-bottom:1px solid var(--border);
       font-size:.92em; color:var(--muted); }
.nav a { margin-right:1em; }
"""


def _build_nav(nav_spec):
    if not nav_spec:
        return ""
    pairs = []
    for chunk in nav_spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        href, label = (chunk.split(":", 1) if ":" in chunk else (chunk, chunk))
        pairs.append(f'<a href="{href.strip()}">{label.strip()}</a>')
    return '<div class="nav"><a href="index.html">← Index</a>' + "".join(pairs) + "</div>" if pairs else ""


def render(md_text, title, nav_html=""):
    body = markdown.markdown(md_text, extensions=["extra", "sane_lists", "fenced_code", "tables"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>{BASE_CSS}</style></head>
<body>{nav_html}<h1>{title}</h1>{body}</body></html>"""


def build_index(out_dir):
    pages = sorted(p for p in Path(out_dir).glob("*.html") if p.name != "index.html")
    items = []
    for p in pages:
        text = p.read_text(encoding="utf-8")
        title = text.split("<title>", 1)[1].split("</title>", 1)[0].strip() if "<title>" in text else p.stem
        items.append(f'<li><a href="{p.name}">{title}</a></li>')
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Review — Index</title><style>{BASE_CSS}</style></head>
<body><h1>Review</h1><ul>{"".join(items)}</ul></body></html>"""
    (Path(out_dir) / "index.html").write_text(html, encoding="utf-8")
    print(f"wrote {Path(out_dir) / 'index.html'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?", help="Source .md file")
    ap.add_argument("out", nargs="?", help="Output .html file")
    ap.add_argument("--title", help="Page title (defaults to filename stem)")
    ap.add_argument("--nav", help="Cross-page nav spec: 'a.html:Label A,b.html:Label B'")
    ap.add_argument("--index", metavar="OUT_DIR", help="Build index.html from all *.html in OUT_DIR")
    args = ap.parse_args()
    if args.index:
        build_index(args.index)
        return 0
    if not args.src or not args.out:
        ap.error("src and out are required unless --index is given")
    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    if not src.is_file():
        sys.exit(f"not a file: {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render(src.read_text(encoding="utf-8"), args.title or src.stem, _build_nav(args.nav or "")),
        encoding="utf-8",
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
