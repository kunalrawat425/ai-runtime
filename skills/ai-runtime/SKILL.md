---
name: ai-runtime
description: "Claude CLI checkpoint and auto-recovery. Wraps claude with automatic recovery for rate limits, crashes, and terminal kills. Saves task state (last step, modified files) and resumes with injected context so Claude picks up exactly where it stopped. Triggers on: rate limit, usage limit, claude keeps stopping, auto-resume, checkpoint, recover claude session, claude lost context, resume task."
user-invokable: true
argument-hint: "run|resume|status|recover|attach [args]"
license: MIT
metadata:
  author: Kunal Rawat
  version: 0.1.0
  category: productivity
---

# ai-runtime: Claude CLI Checkpoint & Auto-Recovery

**Invocation:** `/ai-runtime <command> [args]`

## What this skill does

When the user invokes `/ai-runtime`, run the ai-runtime Python script
located at the skill directory. The skill handles:

- Rate limit hits → saves checkpoint → waits → injects context → resumes
- Crashes → same flow with shorter wait
- Terminal kills → detached daemon mode survives, user can reattach
- Manual resume → loads last checkpoint, injects context, continues

## Commands

| Command | What it does |
|---|---|
| `run '<task>'` | Start a task with auto-recovery |
| `run '<task>' --detach` | Run in background (survives terminal kill) |
| `resume [id]` | Resume last (or specific) session |
| `attach [id]` | Attach terminal to a detached session |
| `status` | List all sessions and their state |
| `recover` | Auto-recover any interrupted session |

## Skill behavior

When invoked, determine which subcommand the user wants and run:

```bash
python3 "$(dirname "$0")/ai_runtime.py" <subcommand> [args]
```

### If user says `/ai-runtime` with no args
Show available commands and ask what task they want to run.

### If user says `/ai-runtime run '<task>'`
Execute:
```bash
python3 ~/.claude/skills/ai-runtime/ai_runtime.py run '<task>'
```
Then tail the output in the conversation.

### If user says `/ai-runtime status`
Execute:
```bash
python3 ~/.claude/skills/ai-runtime/ai_runtime.py status
```
Show the result.

### If user says `/ai-runtime resume`
Execute:
```bash
python3 ~/.claude/skills/ai-runtime/ai_runtime.py resume
```

### If user says `/ai-runtime recover`
Execute:
```bash
python3 ~/.claude/skills/ai-runtime/ai_runtime.py recover
```

## CHECKPOINT marker protocol

When running a long task that might hit rate limits, tell Claude to emit
checkpoint markers after each step. Add this to the task prompt:

```
After each significant step, emit:
<!-- CHECKPOINT: brief description of what you just completed -->
```

The ai-runtime recovery engine reads these markers to know exactly where
to resume. Without markers, it falls back to prose extraction (less reliable).

## Configuration

| Env var | Default | Description |
|---|---|---|
| `AI_RUNTIME_WAIT_SECONDS` | `3600` | How long to wait after rate limit before retrying |

## How recovery works

1. `ai-runtime run` wraps `claude --print` and monitors stdout
2. On rate limit → saves checkpoint to `.ai-runtime/sessions/<id>/checkpoint.json`
3. Finds Claude's conversation history at `~/.claude/projects/<slug>/<session>.jsonl`
4. Appends a structured "context restored" user message to that file
5. Calls `claude --continue` — Claude sees the restoration message and resumes
6. Loop until task completes or user stops it

## Error handling

If Claude's session file is not found, falls back to raw `--continue`
(better than nothing, but context may be incomplete).

If injection succeeds, Claude sees:
```
[AI-RUNTIME CONTEXT RESTORED — interrupted by: rate_limit]

You were working on: <original task>
Last completed step: <checkpoint or extracted step>
Files you had modified: <git diff list>

Please resume from where you stopped...
```
