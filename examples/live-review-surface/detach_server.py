"""
detach_server.py — start make-pages-interactive's server.py pre-orphaned so the
parent-death watchdog disables itself.

Usage:
    python detach_server.py <working_dir> [--port 5050] [--idle-timeout 0]

Why this is needed:
    server.py records its PPID at startup and polls every 5 s. If you start it as a
    direct child of a Bash call (Claude's default), the Bash call returns, the kernel
    reparents the server to PID 1, and the watchdog shuts it down within ~10 s — right
    between comment rounds. The double-fork here ensures the server's PPID is already 1
    before the watchdog first fires, so the watchdog disables itself at startup.

Platform note:
    Double-fork is Unix-only (macOS + Linux). On Windows, use:
        subprocess.Popen([...], creationflags=subprocess.DETACHED_PROCESS
                                              | subprocess.CREATE_NEW_PROCESS_GROUP)

License: MIT (same as the make-pages-interactive repository).
"""
import os, sys

# ── Parse args to forward to server.py ──────────────────────────────────────
wd = sys.argv[1] if len(sys.argv) > 1 else "."
port = "5050"
idle = "0"  # 0 = never auto-shutdown; pass a positive int for normal idle timeout

i = 2
while i < len(sys.argv):
    if sys.argv[i] == "--port" and i + 1 < len(sys.argv):
        port = sys.argv[i + 1]
        i += 2
    elif sys.argv[i] == "--idle-timeout" and i + 1 < len(sys.argv):
        idle = sys.argv[i + 1]
        i += 2
    else:
        i += 1

# server.py lives in lib/ relative to this skill's install root
SKILL_ROOT = os.path.expanduser("~/.claude/skills/make-pages-interactive")
server_py = os.path.join(SKILL_ROOT, "lib", "server.py")
log_path = os.path.join(os.path.abspath(wd), "server.log")

if not os.path.isfile(server_py):
    sys.exit(
        f"server.py not found at {server_py}\n"
        "Is make-pages-interactive installed at ~/.claude/skills/make-pages-interactive ?"
    )

# ── Double-fork ──────────────────────────────────────────────────────────────
# First fork: detach from the caller's session.
if os.fork() != 0:
    sys.exit(0)           # first parent exits immediately

os.setsid()               # new session — no controlling terminal

# Second fork: ensures we are not a session leader (can't reacquire a terminal).
# The server is now orphaned; the kernel sets its PPID to 1.
if os.fork() != 0:
    sys.exit(0)           # second parent exits

# We are now the server process (PPID == 1).
# server.py will see PPID == 1 at startup and disable the watchdog.
log = open(log_path, "a")
os.dup2(log.fileno(), 1)  # stdout → log file
os.dup2(log.fileno(), 2)  # stderr → log file

os.execvp(
    "python3",
    ["python3", server_py, wd, "--port", port, "--idle-timeout", idle],
)
