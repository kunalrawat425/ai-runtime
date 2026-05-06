#!/usr/bin/env python3
"""
ai-runtime: Claude CLI checkpoint and auto-recovery wrapper

Commands:
  ai-runtime run '<task>'       Run a task with auto-recovery
  ai-runtime run '<task>' --detach   Run detached (survives terminal kill)
  ai-runtime resume [id]        Resume last (or specific) session
  ai-runtime attach [id]        Attach terminal to detached session
  ai-runtime status             List sessions and their state
  ai-runtime recover            Attempt recovery from any interrupted session
"""
import os
import sys
import json
import time
import uuid
import subprocess
import signal
import re
import select
import fcntl
import threading
from pathlib import Path
from datetime import datetime, date
import zoneinfo

# ── Config ────────────────────────────────────────────────────────────────────

CHECKPOINT_DIR = Path(".ai-runtime/sessions")
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

RATE_LIMIT_PATTERNS = [
    "usage limit reached",
    "rate limit",
    "claude pro usage limit",
    "you've reached your",
    "you've hit your limit",
    "too many requests",
    "quota exceeded",
]

DEFAULT_WAIT_SECONDS = int(os.environ.get("AI_RUNTIME_WAIT_SECONDS", "3600"))
MAX_ATTEMPTS = int(os.environ.get("AI_RUNTIME_MAX_ATTEMPTS", "10"))
IS_WINDOWS = sys.platform == "win32"

# ── Reset time parsing ────────────────────────────────────────────────────────

# Matches: "resets 10pm (America/New_York)" or "resets 10:30pm (UTC)"
_RESET_RE = re.compile(
    r'resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)',
    re.IGNORECASE,
)

def parse_reset_time(line: str):
    """
    Extract reset time from Claude's rate limit message.
    Returns seconds to wait, or None if not parseable.

    Claude outputs: "You've hit your limit · resets 10pm (America/New_York)"
    """
    m = _RESET_RE.search(line)
    if not m:
        return None

    hour_str, minute_str, ampm, tz_name = m.groups()
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    ampm = ampm.lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        return None

    now = datetime.now(tz)
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if reset <= now:
        # Reset time already passed today — must be tomorrow
        from datetime import timedelta
        reset += timedelta(days=1)

    wait = (reset - now).total_seconds()
    # Add 90s buffer — Anthropic sometimes resets slightly late
    return max(0, wait + 90)


def compute_wait(output_lines: list, fallback: int = DEFAULT_WAIT_SECONDS) -> tuple:
    """
    Return (seconds_to_wait, reset_time_str).
    Tries to parse reset time from recent output. Falls back to fixed wait.
    """
    for line in reversed(output_lines[-20:]):
        secs = parse_reset_time(line)
        if secs is not None:
            reset_dt = datetime.now().timestamp() + secs
            reset_str = datetime.fromtimestamp(reset_dt).strftime("%H:%M:%S")
            return secs, f"reset at {reset_str} (parsed from Claude)"
    return fallback, f"{fallback//60}m (fallback — reset time not found in output)"

# ── Session ───────────────────────────────────────────────────────────────────

class Session:
    def __init__(self, session_id: str, task: str, cwd: str = None):
        self.session_id = session_id
        self.task = task
        self.cwd = cwd or str(Path.cwd())
        self.dir = CHECKPOINT_DIR / session_id
        self.checkpoint_file = self.dir / "checkpoint.json"
        self.log_file = self.dir / "output.log"
        self.pid_file = self.dir / "daemon.pid"

    def save(self, last_step: str, files_modified: list, interrupted_by: str) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "session_id": self.session_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "task_description": self.task,
            "last_step": last_step,
            "files_modified": files_modified,
            "interrupted_by": interrupted_by,
            "cwd": self.cwd,
        }
        self.checkpoint_file.write_text(json.dumps(checkpoint, indent=2))
        return checkpoint

    def load(self):
        if self.checkpoint_file.exists():
            return json.loads(self.checkpoint_file.read_text())
        return None

    def write_pid(self, pid: int):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(pid))

    def read_pid(self):
        if self.pid_file.exists():
            try:
                return int(self.pid_file.read_text().strip())
            except ValueError:
                return None
        return None

    def is_alive(self) -> bool:
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

# ── Claude session file discovery ─────────────────────────────────────────────

def find_claude_session_file(cwd):
    """
    Find the most recent Claude conversation JSONL for this project dir.
    Strategy:
    1. Exact slug match for cwd
    2. Walk up parent dirs (handles cwd-changed case)
    3. Fall back to most recently modified JSONL across ALL projects (last 30 min)
    """
    cwd = Path(cwd)

    # Try exact cwd and parents (up to 3 levels)
    for directory in [cwd] + list(cwd.parents)[:3]:
        slug = str(directory).replace("/", "-")
        project_dir = CLAUDE_PROJECTS_DIR / slug
        if project_dir.exists():
            files = [
                f for f in project_dir.glob("*.jsonl")
                if f.is_file() and f.parent == project_dir
            ]
            if files:
                return max(files, key=lambda f: f.stat().st_mtime)

    # Fallback: most recently touched JSONL across all projects, within 30 min
    cutoff = time.time() - 1800
    candidates = [
        f for f in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl")
        if f.is_file() and f.stat().st_mtime > cutoff
    ]
    if candidates:
        return max(candidates, key=lambda f: f.stat().st_mtime)

    return None

def inject_context(session_file, checkpoint):
    """
    Append a context-restoration user message to Claude's conversation history.
    Claude's --continue reads this file and will see the injected message.
    """
    if not session_file or not session_file.exists():
        return False

    files_str = ", ".join(checkpoint.get("files_modified", [])) or "none"
    last_step = checkpoint.get("last_step", "unknown")
    task = checkpoint.get("task_description", "")
    interrupted_by = checkpoint.get("interrupted_by", "unknown")

    context_msg = {
        "parentUuid": None,
        "isSidechain": False,
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"[AI-RUNTIME CONTEXT RESTORED — interrupted by: {interrupted_by}]\n\n"
                "⚠️ SESSION RESUMED BY AI-RUNTIME — DO NOT START OVER ⚠️\n\n"
                f"Task: {task}\n\n"
                f"Last completed step: {last_step}\n\n"
                f"Files already modified: {files_str}\n\n"
                "INSTRUCTIONS: You were interrupted mid-task (reason: "
                f"{interrupted_by}). The work above is ALREADY DONE. "
                "Pick up exactly where you left off. "
                "Do NOT re-read files you already processed. "
                "Do NOT restart from the beginning. "
                "Continue with the NEXT step after the last completed step. "
                "If you are unsure what the next step is, say so in one sentence "
                "and ask — do not restart the whole task."
            ),
        },
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "uuid": str(uuid.uuid4()),
    }

    try:
        with open(session_file, "a") as f:
            f.write(json.dumps(context_msg) + "\n")
        return True
    except OSError:
        return False

# ── Detection & extraction ────────────────────────────────────────────────────

def detect_rate_limit(line):
    lower = line.lower()
    return any(p in lower for p in RATE_LIMIT_PATTERNS)

def extract_last_step(output_lines):
    """
    Extract the last meaningful step from Claude's output.
    Priority: <!-- CHECKPOINT: ... --> markers > last prose sentence.
    """
    # 1. Look for explicit checkpoint markers (most reliable)
    for line in reversed(output_lines):
        m = re.search(r'<!--\s*CHECKPOINT:\s*(.+?)\s*-->', line)
        if m:
            return m.group(1).strip()[:300]

    # 2. Fallback: strip markdown, find last prose sentence
    cleaned = []
    in_code_block = False
    for line in output_lines[-100:]:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if not in_code_block and stripped and not stripped.startswith("#"):
            cleaned.append(stripped)

    if cleaned:
        last_block = " ".join(cleaned[-5:])
        # Split on sentence-ending punctuation NOT followed by a file extension
        # e.g. "Fixed auth.ts." ends sentence; "auth.ts is done" does not
        sentences = re.findall(r'[A-Z][^!?]*?(?:\.[a-z]{2,4})*[.!?](?=\s|$)', last_block)
        if sentences:
            return sentences[-1][:300]
        return last_block[:300]

    return "step unknown — context limited"

def get_modified_files():
    """Return files modified since last git commit."""
    files = set()
    for git_args in [["git", "diff", "--name-only", "HEAD"], ["git", "diff", "--name-only"]]:
        try:
            r = subprocess.run(git_args, capture_output=True, text=True, timeout=5)
            files.update(f for f in r.stdout.strip().split("\n") if f)
        except Exception:
            pass
    return sorted(files)

# ── Core runner ───────────────────────────────────────────────────────────────

def run_with_recovery(
    initial_args: list[str],
    task: str,
    session: Session,
    log_file=None,
):
    """
    Run claude with automatic recovery loop.
    Handles: rate limit, crash, ctrl-C.
    Writes output to log_file if provided (detached mode).
    """
    args = initial_args
    output_lines = []
    attempt = 0

    def emit(line: str):
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()
        else:
            print(line)

    while True:
        attempt += 1
        if attempt > MAX_ATTEMPTS:
            emit(f"[ai-runtime] max attempts ({MAX_ATTEMPTS}) reached. stopping.")
            emit(f"[ai-runtime] resume manually: ai-runtime resume {session.session_id}")
            return 1
        emit(f"\n[ai-runtime] attempt {attempt}/{MAX_ATTEMPTS} | session {session.session_id}")

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if log_file:
            session.write_pid(proc.pid)

        interrupted_by = None

        try:
            for line in proc.stdout:
                line = line.rstrip()
                emit(line)
                output_lines.append(line)
                if detect_rate_limit(line):
                    interrupted_by = "rate_limit"
                    proc.terminate()
                    break
        except KeyboardInterrupt:
            interrupted_by = "user_ctrl_c"
            proc.terminate()

        exit_code = proc.wait()

        if exit_code == 0 and interrupted_by is None:
            emit("\n[ai-runtime] task completed successfully.")
            return 0

        if interrupted_by is None:
            interrupted_by = "crash"

        # Save checkpoint
        last_step = extract_last_step(output_lines)
        files_modified = get_modified_files()
        checkpoint = session.save(last_step, files_modified, interrupted_by)

        emit(f"[ai-runtime] interrupted: {interrupted_by}")
        emit(f"[ai-runtime] last step: {last_step}")
        emit(f"[ai-runtime] checkpoint: {session.checkpoint_file}")

        if interrupted_by == "user_ctrl_c":
            emit(f"[ai-runtime] stopped. resume with: ai-runtime resume {session.session_id}")
            return 1

        # Inject context into Claude's history
        session_file = find_claude_session_file(Path(session.cwd))
        if session_file:
            ok = inject_context(session_file, checkpoint)
            emit(f"[ai-runtime] context {'injected into ' + session_file.name if ok else 'injection failed — using raw --continue'}")
        else:
            emit("[ai-runtime] claude session file not found — using raw --continue")

        # Wait before resuming
        if interrupted_by == "rate_limit":
            wait, wait_reason = compute_wait(output_lines)
            emit(f"[ai-runtime] waiting until reset — {wait_reason}")
            deadline = time.time() + wait
            while time.time() < deadline:
                remaining = int(deadline - time.time())
                if not log_file:
                    print(
                        f"[ai-runtime] resuming in {remaining//60}m {remaining%60}s...",
                        end="\r", flush=True,
                    )
                time.sleep(15)
            if not log_file:
                print()
        elif interrupted_by == "crash":
            emit("[ai-runtime] process crashed — retrying in 10s...")
            time.sleep(10)

        # Switch to --continue for all subsequent attempts
        args = ["claude", "--continue"]

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_run(task: str, extra_args: list[str], detach: bool = False):
    session_id = uuid.uuid4().hex[:8]
    session = Session(session_id, task)
    session.dir.mkdir(parents=True, exist_ok=True)

    print(f"[ai-runtime] session: {session_id}")
    print(f"[ai-runtime] task: {task}")

    # Augment task with checkpoint marker instructions
    instrumented_task = (
        f"{task}\n\n"
        "---\n"
        "Note: After each significant step, emit a marker on its own line:\n"
        "<!-- CHECKPOINT: brief description of what you just completed -->\n"
        "This lets ai-runtime resume you accurately if interrupted."
    )

    initial_args = ["claude", "--print", instrumented_task] + extra_args

    if detach:
        log_path = session.log_file
        print(f"[ai-runtime] detached mode — output: {log_path}")
        print(f"[ai-runtime] attach with: ai-runtime attach {session_id}")

        if IS_WINDOWS:
            # Windows: spawn new detached process
            import subprocess as _sp
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            DETACHED_PROCESS = 0x00000008
            proc = _sp.Popen(
                [sys.executable, __file__, "_daemon", session_id, task, log_path] + extra_args,
                creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
                close_fds=True,
            )
            session.write_pid(proc.pid)
            print(f"[ai-runtime] daemon pid: {proc.pid}")
        else:
            pid = os.fork()
            if pid == 0:
                os.setsid()
                with open(log_path, "w") as lf:
                    sys.exit(run_with_recovery(initial_args, task, session, log_file=lf))
            else:
                session.write_pid(pid)
                print(f"[ai-runtime] daemon pid: {pid}")

        time.sleep(0.5)
        _tail_log(log_path, follow=True, session=session)

    else:
        return run_with_recovery(initial_args, task, session)


def cmd_resume(session_id=None):
    if session_id:
        session_dir = CHECKPOINT_DIR / session_id
    else:
        if not CHECKPOINT_DIR.exists():
            print("[ai-runtime] no checkpoints found.")
            return 1
        dirs = sorted(
            (d for d in CHECKPOINT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not dirs:
            print("[ai-runtime] no checkpoints found.")
            return 1
        session_dir = dirs[0]
        session_id = session_dir.name

    checkpoint_file = session_dir / "checkpoint.json"
    if not checkpoint_file.exists():
        print(f"[ai-runtime] checkpoint not found: {checkpoint_file}")
        return 1

    checkpoint = json.loads(checkpoint_file.read_text())
    session = Session(session_id, checkpoint["task_description"], checkpoint.get("cwd"))

    print(f"[ai-runtime] resuming: {session_id}")
    print(f"[ai-runtime] task: {checkpoint['task_description']}")
    print(f"[ai-runtime] last step: {checkpoint.get('last_step', 'unknown')}")

    # Inject context
    session_file = find_claude_session_file(Path(checkpoint.get("cwd", ".")))
    if session_file:
        ok = inject_context(session_file, checkpoint)
        print(f"[ai-runtime] context {'injected' if ok else 'injection failed — using raw --continue'}")
    else:
        print("[ai-runtime] session file not found — using raw --continue")

    return run_with_recovery(["claude", "--continue"], checkpoint["task_description"], session)


def cmd_attach(session_id=None):
    if session_id:
        session_dir = CHECKPOINT_DIR / session_id
    else:
        if not CHECKPOINT_DIR.exists():
            print("[ai-runtime] no sessions found.")
            return 1
        dirs = sorted(
            (d for d in CHECKPOINT_DIR.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if not dirs:
            print("[ai-runtime] no sessions found.")
            return 1
        session_dir = dirs[0]
        session_id = session_dir.name

    log_path = session_dir / "output.log"
    cp_file = session_dir / "checkpoint.json"

    if not log_path.exists():
        print(f"[ai-runtime] no log found for session {session_id}.")
        return 1

    checkpoint = json.loads(cp_file.read_text()) if cp_file.exists() else {}
    session = Session(session_id, checkpoint.get("task_description", ""), checkpoint.get("cwd"))

    print(f"[ai-runtime] attaching to session {session_id}")
    _tail_log(log_path, follow=True, session=session)
    return 0


def cmd_status():
    if not CHECKPOINT_DIR.exists():
        print("no sessions found.")
        return

    dirs = sorted(
        (d for d in CHECKPOINT_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if not dirs:
        print("no sessions found.")
        return

    header = f"{'ID':<10} {'STATUS':<12} {'INTERRUPTED':<14} {'LAST STEP':<45} {'TIME'}"
    print(header)
    print("-" * len(header))

    for sd in dirs[:15]:
        cp_file = sd / "checkpoint.json"
        pid_file = sd / "daemon.pid"

        if not cp_file.exists():
            continue

        cp = json.loads(cp_file.read_text())
        last_step = cp.get("last_step", "?")[:43]
        interrupted = cp.get("interrupted_by", "?")
        ts = cp.get("timestamp", "?")[:16]

        # Determine status
        pid = None
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                status = "running"
            except (ValueError, OSError):
                status = "stopped"
        else:
            status = "stopped"

        print(f"{sd.name:<10} {status:<12} {interrupted:<14} {last_step:<45} {ts}")


def cmd_recover():
    """Attempt to resume the most recently interrupted session."""
    if not CHECKPOINT_DIR.exists():
        print("[ai-runtime] no sessions to recover.")
        return 1

    dirs = sorted(
        (d for d in CHECKPOINT_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    for sd in dirs:
        cp_file = sd / "checkpoint.json"
        pid_file = sd / "daemon.pid"

        if not cp_file.exists():
            continue

        # Skip if already running
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                print(f"[ai-runtime] session {sd.name} is already running (pid {pid}).")
                continue
            except (ValueError, OSError):
                pass

        print(f"[ai-runtime] recovering session: {sd.name}")
        return cmd_resume(sd.name)

    print("[ai-runtime] no interrupted sessions found.")
    return 0

# ── Helpers ───────────────────────────────────────────────────────────────────

def _tail_log(log_path: Path, follow: bool, session=None):
    """Tail a log file, optionally following new content."""
    with open(log_path) as f:
        # Print existing content
        sys.stdout.write(f.read())
        sys.stdout.flush()

        if not follow:
            return

        print("[ai-runtime] following log (ctrl-c to detach) ...")
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    # Check if daemon is still alive
                    if session and not session.is_alive():
                        print("\n[ai-runtime] daemon finished.")
                        break
                    time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[ai-runtime] detached (session continues in background).")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "run":
        if not rest:
            print("usage: ai-runtime run '<task>' [--detach] [claude args...]")
            return 1
        task = rest[0]
        detach = "--detach" in rest
        extra = [a for a in rest[1:] if a != "--detach"]
        return cmd_run(task, extra, detach=detach) or 0

    elif cmd == "resume":
        return cmd_resume(rest[0] if rest else None) or 0

    elif cmd == "attach":
        return cmd_attach(rest[0] if rest else None) or 0

    elif cmd == "status":
        cmd_status()
        return 0

    elif cmd == "recover":
        return cmd_recover() or 0

    elif cmd == "_daemon":
        # Internal: Windows detached process entry point
        # args: _daemon <session_id> <task> <log_path> [extra...]
        if len(rest) < 3:
            return 1
        _sid, _task, _log = rest[0], rest[1], rest[2]
        _extra = rest[3:]
        _session = Session(_sid, _task)
        _instrumented = (
            f"{_task}\n\n---\n"
            "Note: After each significant step, emit a marker on its own line:\n"
            "<!-- CHECKPOINT: brief description of what you just completed -->\n"
            "This lets ai-runtime resume you accurately if interrupted."
        )
        _args = ["claude", "--print", _instrumented] + _extra
        with open(_log, "w") as lf:
            return run_with_recovery(_args, _task, _session, log_file=lf)

    else:
        print(f"unknown command: {cmd}")
        print("commands: run, resume, attach, status, recover")
        return 1


if __name__ == "__main__":
    sys.exit(main())
