---
name: make-pages-interactive
description: Turn a directory of static HTML pages into a live commenting surface. Injects a feedback library, starts a tiny server, and routes user comments into a JSONL inbox that the agent monitors and responds to by editing the pages. Trigger phrases — "make this page interactive", "make these pages interactive", "let me comment on this page", "add feedback to these pages".
---

# Make Pages Interactive

Turns any folder of HTML files into a place the user can leave inline comments on (text selections, element selections, page-level notes). Comments POST to a local JSONL inbox; you (the agent) Monitor that inbox, edit the HTML in response, append to `feedback/history.json`, and the page auto-reloads with a walkthrough of what changed.

## When to invoke

User says any of:
- "make this page interactive" / "make these pages interactive" → **Setup flow**
- "add feedback to this page" / "let me comment on this page" → **Setup flow**
- "set up feedback on <dir>" → **Setup flow**
- "stop the feedback server" / "kill the server" / "shut it down" → **Stop flow**
- "remove the feedback layer" / "make pages static again" → **Removal flow**
- "update the make-pages-interactive skill" → **Update flow**

## Setup flow (when user wants to make pages interactive)

1. **Identify the target directory.** Usually the user's current working directory or a folder they named. If ambiguous, ask.
2. **Inject the feedback tags** into every `*.html` in that directory:
   ```
   python ~/.claude/skills/make-pages-interactive/scripts/inject.py <dir>
   ```
   Add `--recursive` if the pages live in subfolders. The script is idempotent — safe to re-run. It also creates `<dir>/feedback/inbox.jsonl` and `<dir>/feedback/history.json` if missing.
3. **Pick a port.** Default 5050. Before starting, check what's there:
   ```
   curl -s --max-time 2 http://localhost:5050/info
   ```
   - JSON with `artifact_dir` matching this `<dir>` → reuse it, skip to step 5.
   - JSON with a *different* `artifact_dir` → port is held by another exploration. Either ask the user to free it (`lsof -ti:5050 | xargs kill`) or use port 5051, 5052, … (try the next port; tell the user the URL).
   - No response → port 5050 is free.
4. **Start the server in the background** via Bash with `run_in_background: true`:
   ```
   python ~/.claude/skills/make-pages-interactive/lib/server.py <dir> --port <chosen>
   ```
   The server auto-shuts-down on parent death or 10 min of idle, so you don't need to manage its lifecycle.
5. **Tell the user the URL.** For example: `http://localhost:5050/index.html` (use whatever filename they actually have — `index.html`, `report.html`, etc.). If they have multiple pages, list the top-level ones.
6. **Start a Monitor on the inbox** so new comments notify you immediately:
   ```
   Monitor on path: <dir>/feedback/inbox.jsonl
   ```
   Do NOT poll — let the Monitor notification arrive.

## Responding to a feedback batch

### ⚠ Critical: one Monitor event = one inbox line = potentially MANY comments

The user submits feedback by clicking "Send" once in the page, which appends **one line** to `inbox.jsonl`. That single line is a JSON object whose `comments` field is an **array**. The user may have left 1, 3, or 15 comments before clicking Send — they all arrive in that one array.

The Monitor notification you receive is a **preview**, not the full payload. It typically surfaces only the most recently submitted comment's text (the last item in the array), and truncates everything else. **Acting on the preview alone will miss everything else in the batch.** Past failure mode: agent reads the Monitor text, edits one thing, declares done — silently ignores 10+ other comments in the same submission.

### Required process

For every Monitor event:

1. **Read the full inbox.jsonl** (or at least `tail -1`) — don't trust the Monitor preview text. Parse the most recently appended JSON line.
2. **Enumerate `entry.comments[]`** — get the count and the full list of comment ids. State the count out loud (e.g. "incoming batch: 12 comments") so you can verify completeness at the end.
3. **For each comment in the array**, edit the relevant HTML to address it. Comments may be `type: "general"` (page-level), `type: "elements"` (anchored to a specific DOM element via selector + outer_html), or `type: "text"` (anchored to a text-selection quote). Address all of them — even vague ones; at minimum acknowledge them in the response or list them as "needs clarification" in the change description.
4. **Wrap each modified region** with `<span data-cf-change="ch-<short-slug>">…</span>` or add `data-cf-change` to an existing wrapping element. One anchor per logical change (multiple comments may collapse into one change; one comment may produce several changes — both are fine).
5. **Append ONE new batch object** to the end of `<dir>/feedback/history.json`. Schema:
   ```json
   {
     "batch_id": "b-<timestamp-or-slug>",
     "timestamp": "<ISO 8601>",
     "comments": [ /* echo back ALL the inbox comments you addressed */ ],
     "changes": [
       {
         "id": "ch-<slug>",
         "in_response_to": ["<cf_id>", "<cf_id>", ...],
         "anchor": "ch-<slug>",
         "title": "short, concrete",
         "description": "longer prose"
       }
     ]
   }
   ```
6. **Verify completeness before declaring done.** Compute `union(change.in_response_to for change in changes)` and compare to the set of comment ids from the inbox entry. They must be equal (or you must explicitly document any skipped ids in the description, e.g. "duplicate of c-…" or "needs clarification — not yet addressed"). If you find a gap, fix the gap before posting the batch.

A correct response to a 12-comment batch produces:
- 12 (or fewer, if collapsed) change entries
- Every one of the 12 comment ids appears in at least one `in_response_to` array
- Every change has a matching `data-cf-change` anchor present in the actual HTML

### Other notes

- Each comment has a stable `cf_id`. Selectors point to the exact element/text the user commented on. The selector format `[data-cf-id="el-N"]` is a runtime-only attribute the widget assigns — don't rely on it persisting after your edits. The `outer_html` snippet in the comment is the most reliable way to identify the element you should change.
- The page polls `history.json`, sees the new batch, auto-reloads (scroll position preserved), and offers the user a walkthrough of the changes. The "processing…" chip on each submitted comment clears automatically when any `in_response_to` matches that comment's id — so completeness in step 6 directly determines whether the user sees their comments marked as resolved.
- If a comment is genuinely ambiguous, address what you can and write the ambiguity into the change `description`. Don't silently skip.
- If a comment asks for something that requires a structural change spanning many slides / pages, still respond in the same batch — just produce multiple changes with multiple anchors, all linking back to that one comment id.

## On startup in a directory that already has feedback

If you find `<dir>/feedback/inbox.jsonl` and `<dir>/feedback/history.json` and the skill has been invoked in this session:
1. Scan inbox for comment ids.
2. Scan history's `changes[*].in_response_to` union — those are already processed.
3. If unprocessed comments exist, tell the user the count and ask whether to process now.
4. Either way, set up the Monitor on the inbox.

## Stop flow (user wants to kill the server)

1. Identify the port. If you started the server in this session, you know it. Otherwise check `curl -s http://localhost:5050/info` (try 5051, 5052 if 5050 returns nothing or a different artifact).
2. Kill it: `lsof -ti:<port> | xargs kill` (use `kill -9` only if a plain kill doesn't free the port within a few seconds — the server traps SIGTERM and exits cleanly).
3. Confirm: `lsof -i :<port>` should be silent.
4. If you also started a `Monitor` on the inbox in this session, it will keep watching the file — that's fine, the file just won't get new entries.

Note: in most cases the user doesn't need to manually stop the server. It auto-shuts-down when (a) the parent process dies (e.g. they close the Claude Code window — within ~5–10 s) or (b) no client requests for 10 min. Manual stop is for the case where they want the port back *right now* in the same session.

## Update flow (user wants the latest lib/)

```
python ~/.claude/skills/make-pages-interactive/scripts/update.py
```
Runs `git pull --ff-only` inside the skill dir. Requires git-clone install (the script tells the user how to re-install if not).

## Removal flow (clean static copy)

If the user wants their HTML back to a clean, server-independent state:
```
python ~/.claude/skills/make-pages-interactive/scripts/inject.py <dir> --remove
```
Strips both tags from every `*.html`. Leaves the `feedback/` directory alone (delete manually if not wanted).

## Files in this skill

```
~/.claude/skills/make-pages-interactive/
├── SKILL.md              # this file (agent-facing)
├── README.md             # GitHub-facing docs (human readers)
├── LICENSE
├── lib/
│   ├── feedback.js       # client library: selection + commenting + tour
│   ├── feedback.css      # styles
│   └── server.py         # stdlib-only HTTP server
└── scripts/
    ├── inject.py         # idempotent tag injection / removal
    └── update.py         # git pull --ff-only
```

## Gotchas

- The injected `<link>` and `<script>` reference absolute paths `/lib/feedback.css` and `/lib/feedback.js`. These resolve through `server.py`, which routes `/lib/*` to the skill's own `lib/` directory. So pages only work when opened through this server — opening the HTML file directly in a browser will silently fail to load the feedback widget (the page itself still renders).
- `history.json` order matters: append (don't prepend). The library walks from the end to find the latest batch for the walkthrough.
- `anchor` values must match a `data-cf-change` attribute actually present in the HTML. Typos here cause "anchor not found" warnings post-reload.
