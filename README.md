# make-pages-interactive

A Claude Code skill that turns any folder of static HTML pages into a **live commenting surface**. Highlight text, click an element, leave a note — the comment lands in a local inbox that Claude reads and responds to by editing the page. The page auto-reloads with a walkthrough of what changed.

Originally built for iterating on research artifacts (long HTML reports with plots, tables, explanations) but works for any folder of HTML: docs, design mocks, generated reports, prototype UIs.

![Screenshot of make-pages-interactive in action](screenshot.png)

---

## How it works

```
                  ┌──────────────────┐
   user highlights│   feedback.js    │   POST /feedback
   / clicks  ───▶ │  (in every page) │ ───────────────┐
                  └──────────────────┘                 ▼
                                              ┌────────────────┐
                  ┌──────────────────┐  poll  │   server.py    │
   page reloads ◀─│   feedback.js    │ ◀───── │  (stdlib HTTP) │
   with walkthru  └──────────────────┘history │                │
                                              └───────┬────────┘
                                                      │ append
                                          ┌───────────▼────────────┐
                                          │  feedback/inbox.jsonl  │
                                          └───────────┬────────────┘
                                                      │ Monitor
                                                      ▼
                                          ┌────────────────────────┐
                                          │  Claude (the agent)    │
                                          │  edits HTML, appends   │
                                          │  feedback/history.json │
                                          └────────────────────────┘
```

The skill is **just three pieces**:

| File | Role |
|------|------|
| `lib/feedback.js` | Client library injected into every page. Handles text selection, element selection, comment editor, page-reload walkthrough. |
| `lib/feedback.css` | Styles for the comment UI. |
| `lib/server.py` | ~250-line stdlib-only HTTP server. Serves the page directory, accepts comment POSTs, serves the lib/ files from `/lib/*`. Auto-shuts-down on parent death or 10 min of idle so it doesn't leak processes. |

Plus glue:

| File | Role |
|------|------|
| `SKILL.md` | What Claude Code reads to know when and how to invoke the skill. |
| `scripts/inject.py` | Idempotently injects (or removes) the two `<link>`/`<script>` tags in every `*.html` in a directory. |
| `scripts/update.py` | `git pull --ff-only` inside the skill directory. |

---

## Install

```bash
git clone https://github.com/paraschopra/make-pages-interactive \
  ~/.claude/skills/make-pages-interactive
```

That's it. Claude Code auto-discovers any folder under `~/.claude/skills/` that contains a `SKILL.md`.

Updates are explicit:

```bash
python ~/.claude/skills/make-pages-interactive/scripts/update.py
```

Or just say "update the make-pages-interactive skill" in Claude Code.

---

## Usage

Inside any Claude Code session, say:

> "Make these pages interactive."

(or any of: "make this page interactive", "let me comment on this page", "add feedback to these pages")

Claude will:

1. Inject the feedback library tags into every `*.html` in the current directory.
2. Create `feedback/inbox.jsonl` and `feedback/history.json`.
3. Pick a free port (5050 by default, falls back if taken).
4. Start the server in the background.
5. Tell you the URL to open.
6. Start monitoring the inbox so any comment you leave gets picked up immediately.

Open the URL. Comment away. Claude edits the page in response.

### Removing the feedback layer

To get a clean static copy back (no `/lib/` dependencies in the HTML):

```bash
python ~/.claude/skills/make-pages-interactive/scripts/inject.py ./your-dir --remove
```

Or say "remove the feedback layer from these pages."

---

## How the server shuts down

The server is designed to never leak — three ways it goes away:

1. **Parent-process death** *(automatic, ~5–10 s)*. The server records its parent PID at startup and polls every 5 s. When the parent dies (e.g., you close the Claude Code window that launched it), the kernel reparents the server to PID 1 — the watchdog notices and calls `os._exit(0)`. Skipped if the server was started detached at launch (parent was already PID 1, e.g. `nohup`).

2. **Idle timeout** *(automatic, default 10 min)*. The page polls `/feedback/history.json` every ~4 s, so any open browser tab keeps the server alive. When no client requests have arrived for `--idle-timeout` seconds (default `600`), the server exits. Pass `--idle-timeout 0` to disable.

3. **Manual stop**. Either:
   - Say "stop the feedback server" in your Claude Code session — Claude runs `lsof -ti:5050 | xargs kill` (adjust the port if you used a non-default one).
   - Or hit `Ctrl-C` in the terminal where the server is logging.

You generally don't need to think about this. The auto-shutdowns mean abandoned servers self-clean — close your Claude window and within ~10 s the port is free again.

---

## Comment types

The library supports three commenting modes:

- **Text selection** — highlight any text, a popup offers "comment on selection".
- **Element selection** — click the "select element" tool, then click an image, table, section. Comment is anchored to a stable selector.
- **Page-level** — a floating button leaves notes that aren't tied to any specific element.

Each comment carries a stable `cf_id`, a selector describing what was pointed at, the comment body, and a timestamp. The library batches comments client-side and submits as a single POST so Claude responds to a coherent set rather than firing on every keystroke.

---

## When Claude responds

When you submit a batch:

1. A "processing…" banner appears at the top of the page.
2. Your tab title changes to `🔔 …` so you can see progress in a backgrounded tab.
3. Claude edits the relevant HTML, appends an entry to `feedback/history.json` that maps your comment ids → the changes made.
4. The page polls `history.json` every ~4 seconds, notices the new entry, and auto-reloads — preserving your scroll position.
5. Post-reload, a walkthrough appears highlighting each changed region with the title Claude gave it. Press `R` to dismiss; the changes stay in the history sidebar.

---

## Repo layout

```
make-pages-interactive/
├── SKILL.md              # Agent-facing skill spec
├── README.md             # This file
├── screenshot.png        # README screenshot
├── LICENSE
├── lib/
│   ├── feedback.js
│   ├── feedback.css
│   └── server.py
└── scripts/
    ├── inject.py
    └── update.py
```

---

## Why this exists

I kept building long HTML research reports and wanting to leave inline notes on them — "expand this section", "this plot is misleading", "what about edge case X?" — without breaking out of the page to write a separate to-do list. This skill turns that into a one-liner: every page is now a place I can scribble on, and Claude turns the scribbles into edits.

The same workflow works for design docs, generated dashboards, code walkthroughs, anything that lives as HTML.

---

## License

MIT. See [LICENSE](LICENSE).
