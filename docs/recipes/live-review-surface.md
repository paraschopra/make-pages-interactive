# Recipe: Build a Live Review Surface for Agent-Generated Outputs

*A step-by-step guide for using `make-pages-interactive` to turn routine markdown outputs into a
persistent, live commenting surface — including the detached-server pattern and active watch loop
that the base skill leaves as an exercise.*

---

## The gap this recipe fills

The base skill's README shows the happy path: you have a folder of HTML, you say "make these pages
interactive," and Claude injects the library, starts the server, and watches. What's underspecified:

- **How to survive across multiple comment rounds.** The server has a parent-death watchdog. If
  launched as a child of a Bash call that returns, the server gets reparented and the watchdog shuts
  it down within ~10 s — right between comment rounds.
- **How to keep Claude watching without re-prompting.** After the server starts, comments land in
  `feedback/inbox.jsonl` but nothing processes them unless Claude is actively monitoring. The skill
  doesn't prescribe a pattern for the persistent watch loop.
- **How to render markdown → HTML in a consistent, scannable style** before injecting the feedback
  layer.

This recipe covers all three. The four helper files live in
[`examples/live-review-surface/`](../../examples/live-review-surface/).

---

## Step 1 — Render markdown → HTML

If your outputs are markdown, convert them to HTML before injecting the feedback library. A minimal
renderer (`render.py`) uses the `markdown` package and needs no external framework:

```bash
pip3 install --user markdown
```

**Render a single file:**
```bash
python render.py report.md output/report.html --title "My Report"
```

**Render several files with cross-page nav:**
```bash
python render.py doc1.md output/doc1.html --title "Doc 1" --nav "doc2.html:Doc 2,doc3.html:Doc 3"
python render.py doc2.md output/doc2.html --title "Doc 2" --nav "doc1.html:Doc 1,doc3.html:Doc 3"
python render.py --index output/   # generates index.html linking to all *.html
```

Then inject and serve as normal (Step 2 onwards). If your outputs are already HTML, skip Step 1.

---

## Step 2 — Start the server DETACHED

This is the critical pattern. The server has a parent-death watchdog: it records its PPID at
startup and polls every 5 s. If launched as a direct child of a Bash call (Claude's default), the
call returns, the kernel reparents the server to PID 1, and the watchdog shuts it down — right
between comment rounds.

**The fix: double-fork so the server is already orphaned (PPID=1) before the watchdog fires.**

`detach_server.py` handles this. Usage:

```bash
python detach_server.py ./output --port 5050 --idle-timeout 0
sleep 1
```

**Verify it's running and orphaned:**
```bash
lsof -ti:5050                    # prints the PID
ps -o ppid= -p $(lsof -ti:5050) # must print 1
```

If PPID is 1, the server will survive across comment rounds until you stop it manually:
```bash
lsof -ti:5050 | xargs kill
```

> **Windows note.** The double-fork is Unix-only (macOS + Linux). On Windows, replace the
> `os.fork()` calls with
> `subprocess.Popen([...], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)`.

---

## Step 3 — Arm the active watch loop

Starting the server is not enough. Comments land in `feedback/inbox.jsonl` but nothing acts on
them until Claude reads the file. After the server is confirmed up, arm a persistent `Monitor` on
the inbox so every submitted comment batch wakes Claude to process it:

```
Monitor(
  persistent=true,
  description="review: new comment batches",
  command="tail -n0 -F <working_dir>/feedback/inbox.jsonl"
)
```

Each new line in `inbox.jsonl` is one submitted batch (all comments from a single click). Claude
processes it, edits the HTML, appends to `feedback/history.json`, and goes back to watching. The
page auto-reloads and shows the walkthrough.

**If `Monitor` is unavailable** in your session (older Claude Code, restricted context): tell the
user explicitly — "I'm not watching live — ping me after you comment and I'll process the batch."
The loop degrades gracefully to manual round-trips; nothing breaks.

---

## Step 4 — (Optional) Visual edit cues

After Claude edits HTML in response to a comment, wrapping the changed region with
`<span data-cf-change="ch-<slug>">…</span>` lets the page show a persistent left-gutter bar and a
numbered floating "Changes (N)" chip. The CSS + JS (`overlay.css` / `overlay.js`) are self-contained
and layer on top of `make-pages-interactive`'s own feedback styles without touching them.

**How Claude marks an edit:**
```html
<span data-cf-change="ch-your-slug">…edited content…</span>
```

The slug becomes the change title in the chip (hyphens → spaces, first char uppercased). Keep
slugs short and kebab-case: `ch-section-rewritten`, `ch-table-clarified`.

To use, add to each rendered HTML page:
```html
<link rel="stylesheet" href="overlay.css">
<!-- before </body>: -->
<script src="overlay.js"></script>
```

Or inline the contents directly into the page's `<head>` / before `</body>`.

---

## Full spin-up sequence (all four steps together)

```bash
# 1. Render your markdown
python render.py report.md output/report.html --title "Report"
python render.py --index output/

# 2. Inject the feedback library
python ~/.claude/skills/make-pages-interactive/scripts/inject.py output/

# 3. Start the server detached
python detach_server.py output/ --port 5050 --idle-timeout 0
sleep 1
lsof -ti:5050   # confirm it's up

# 4. Open in browser
open http://localhost:5050/index.html

# 5. Arm Monitor in the Claude session
Monitor(persistent=true, description="watch inbox", command="tail -n0 -F output/feedback/inbox.jsonl")
```

Now: highlight text → leave a note → Claude edits → page auto-reloads → repeat.

---

## Notes & caveats

- **`--idle-timeout 0`**: disables auto-shutdown. Use `lsof -ti:5050 | xargs kill` to stop
  manually when done.
- **`render.py` is optional**: skip Step 1 if your outputs are already HTML.
- **`Monitor` + `persistent=true`**: tested in Claude Code ≥ 1.x. In older versions, replace with
  a manual poll loop or prompt the user to re-invoke after commenting.
- **`overlay.css` / `overlay.js`**: purely cosmetic — no dependency on `make-pages-interactive`
  internals. They work on any page with `[data-cf-change]` elements.
- **Attribution**: `render.py`, `detach_server.py`, `overlay.css`, and `overlay.js` were developed
  as part of a personal Claude Code skill workflow and contributed here under the same MIT license
  as this repository.
