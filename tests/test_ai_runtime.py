"""
Tests for ai-runtime core logic.
Run: python3 -m pytest tests/ -v
 OR: python3 -m unittest discover tests/
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Add skill dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "ai-runtime"))
import ai_runtime as rt


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_session(tmpdir, session_id="test123", task="do the thing"):
    rt.CHECKPOINT_DIR = Path(tmpdir) / ".ai-runtime" / "sessions"
    s = rt.Session(session_id, task, cwd=tmpdir)
    return s


# ── Rate limit detection ──────────────────────────────────────────────────────

class TestDetectRateLimit(unittest.TestCase):

    def test_usage_limit_reached(self):
        self.assertTrue(rt.detect_rate_limit("Usage limit reached"))

    def test_claude_pro_limit(self):
        self.assertTrue(rt.detect_rate_limit("Claude Pro Usage Limit reached"))

    def test_youve_hit_your_limit(self):
        self.assertTrue(rt.detect_rate_limit("You've hit your limit · resets 10pm"))

    def test_too_many_requests(self):
        self.assertTrue(rt.detect_rate_limit("Too many requests. Please slow down."))

    def test_quota_exceeded(self):
        self.assertTrue(rt.detect_rate_limit("Quota exceeded for this period"))

    def test_rate_limit_phrase(self):
        self.assertTrue(rt.detect_rate_limit("API rate limit exceeded"))

    def test_case_insensitive(self):
        self.assertTrue(rt.detect_rate_limit("USAGE LIMIT REACHED"))
        self.assertTrue(rt.detect_rate_limit("usage limit reached"))

    def test_normal_output_not_detected(self):
        self.assertFalse(rt.detect_rate_limit("Reading auth.ts..."))
        self.assertFalse(rt.detect_rate_limit("Modifying the config file"))
        self.assertFalse(rt.detect_rate_limit("Done!"))
        self.assertFalse(rt.detect_rate_limit(""))
        self.assertFalse(rt.detect_rate_limit("<!-- CHECKPOINT: migrated auth.ts -->"))

    def test_partial_match_in_sentence(self):
        self.assertTrue(rt.detect_rate_limit(
            "Claude.ai usage limit reached — your session has ended"
        ))


# ── Reset time parsing ────────────────────────────────────────────────────────

class TestParseResetTime(unittest.TestCase):

    def _check_positive(self, line):
        result = rt.parse_reset_time(line)
        self.assertIsNotNone(result, f"Expected parse result for: {line!r}")
        self.assertGreater(result, 0)
        return result

    def _check_none(self, line):
        result = rt.parse_reset_time(line)
        self.assertIsNone(result, f"Expected None for: {line!r}")

    def test_basic_pm_time(self):
        self._check_positive("You've hit your limit · resets 10pm (America/New_York)")

    def test_am_time(self):
        self._check_positive("resets 5am (UTC)")

    def test_with_minutes(self):
        self._check_positive("Resets 11:30pm (America/Los_Angeles)")

    def test_midnight_12am(self):
        result = rt.parse_reset_time("resets 12am (UTC)")
        # 12am = 0:00, should be in the future if not already midnight
        self.assertIsNotNone(result)

    def test_noon_12pm(self):
        result = rt.parse_reset_time("resets 12pm (UTC)")
        self.assertIsNotNone(result)

    def test_no_time_in_message(self):
        self._check_none("Usage limit reached")
        self._check_none("You've hit your limit")
        self._check_none("rate limit")

    def test_invalid_timezone(self):
        result = rt.parse_reset_time("resets 10pm (Not/ATimezone)")
        self.assertIsNone(result)

    def test_future_time_positive(self):
        # Should always return positive seconds for any valid future time
        result = rt.parse_reset_time("resets 11pm (America/New_York)")
        if result is not None:
            self.assertGreater(result, 0)

    def test_90_second_buffer_included(self):
        # Result should include at least 90s buffer
        result = rt.parse_reset_time("resets 11pm (UTC)")
        if result is not None:
            # Hard to test exact buffer, but result should be >= 90
            self.assertGreaterEqual(result, 90)


# ── compute_wait ──────────────────────────────────────────────────────────────

class TestComputeWait(unittest.TestCase):

    def test_parses_from_output_lines(self):
        lines = [
            "Doing some work...",
            "You've hit your limit · resets 10pm (America/New_York)",
        ]
        secs, reason = rt.compute_wait(lines, fallback=999)
        self.assertNotEqual(secs, 999, "Should have parsed reset time, not used fallback")
        self.assertIn("parsed", reason)

    def test_fallback_when_no_reset_time(self):
        lines = ["usage limit reached", "some other line"]
        secs, reason = rt.compute_wait(lines, fallback=1800)
        self.assertEqual(secs, 1800)
        self.assertIn("fallback", reason)

    def test_empty_lines_uses_fallback(self):
        secs, reason = rt.compute_wait([], fallback=500)
        self.assertEqual(secs, 500)

    def test_searches_last_20_lines(self):
        # Put the reset line early — should NOT find it (only last 20 checked)
        early_lines = ["resets 10pm (UTC)"] + ["noise"] * 25
        secs, reason = rt.compute_wait(early_lines, fallback=777)
        self.assertEqual(secs, 777, "Should not parse line outside last 20")

    def test_searches_within_last_20_lines(self):
        lines = ["noise"] * 15 + ["You've hit your limit · resets 10pm (UTC)"]
        secs, reason = rt.compute_wait(lines, fallback=999)
        self.assertNotEqual(secs, 999)


# ── Last step extraction ──────────────────────────────────────────────────────

class TestExtractLastStep(unittest.TestCase):

    def test_checkpoint_marker_takes_priority(self):
        lines = [
            "Doing some work",
            "<!-- CHECKPOINT: migrated auth.ts to JWT -->",
            "Some trailing output",
        ]
        result = rt.extract_last_step(lines)
        self.assertEqual(result, "migrated auth.ts to JWT")

    def test_most_recent_checkpoint_used(self):
        lines = [
            "<!-- CHECKPOINT: step one -->",
            "more work",
            "<!-- CHECKPOINT: step two -->",
        ]
        result = rt.extract_last_step(lines)
        self.assertEqual(result, "step two")

    def test_checkpoint_with_extra_spaces(self):
        result = rt.extract_last_step(["<!--  CHECKPOINT:   done the thing  -->"])
        self.assertEqual(result, "done the thing")

    def test_fallback_to_prose_sentence(self):
        lines = [
            "Reading the configuration file.",
            "Found 3 issues in the code.",
            "Fixed the null pointer in auth.ts.",
        ]
        result = rt.extract_last_step(lines)
        self.assertIn("auth.ts", result)

    def test_strips_code_blocks(self):
        lines = [
            "Here is the code:",
            "```python",
            "def foo(): pass",
            "```",
            "Fixed the function above.",
        ]
        result = rt.extract_last_step(lines)
        # Should not contain code block content
        self.assertNotIn("def foo", result)

    def test_empty_lines_returns_unknown(self):
        result = rt.extract_last_step([])
        self.assertIn("unknown", result)

    def test_result_truncated_at_300(self):
        long_line = "A" * 500
        lines = [f"<!-- CHECKPOINT: {long_line} -->"]
        result = rt.extract_last_step(lines)
        self.assertLessEqual(len(result), 300)

    def test_skips_markdown_headings(self):
        lines = [
            "# Big Heading",
            "## Subheading",
            "This is the actual last step.",
        ]
        result = rt.extract_last_step(lines)
        self.assertIn("last step", result)


# ── Session ───────────────────────────────────────────────────────────────────

class TestSession(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        rt.CHECKPOINT_DIR = Path(self.tmpdir) / ".ai-runtime" / "sessions"

    def test_save_creates_checkpoint_file(self):
        s = make_session(self.tmpdir)
        s.save("did the thing", ["auth.ts", "users.ts"], "rate_limit")
        self.assertTrue(s.checkpoint_file.exists())

    def test_save_checkpoint_content(self):
        s = make_session(self.tmpdir)
        cp = s.save("did the thing", ["auth.ts"], "crash")
        self.assertEqual(cp["last_step"], "did the thing")
        self.assertEqual(cp["files_modified"], ["auth.ts"])
        self.assertEqual(cp["interrupted_by"], "crash")
        self.assertEqual(cp["task_description"], "do the thing")

    def test_load_returns_saved_checkpoint(self):
        s = make_session(self.tmpdir)
        s.save("step one", [], "rate_limit")
        loaded = s.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["last_step"], "step one")

    def test_load_returns_none_when_no_file(self):
        s = make_session(self.tmpdir, session_id="nonexistent")
        self.assertIsNone(s.load())

    def test_write_and_read_pid(self):
        s = make_session(self.tmpdir)
        s.dir.mkdir(parents=True, exist_ok=True)
        s.write_pid(12345)
        self.assertEqual(s.read_pid(), 12345)

    def test_read_pid_returns_none_when_no_file(self):
        s = make_session(self.tmpdir, session_id="nopid")
        self.assertIsNone(s.read_pid())

    def test_is_alive_false_for_dead_process(self):
        s = make_session(self.tmpdir)
        s.dir.mkdir(parents=True, exist_ok=True)
        s.write_pid(999999999)  # almost certainly not a real pid
        self.assertFalse(s.is_alive())

    def test_is_alive_true_for_self(self):
        s = make_session(self.tmpdir)
        s.dir.mkdir(parents=True, exist_ok=True)
        s.write_pid(os.getpid())
        self.assertTrue(s.is_alive())

    def test_checkpoint_has_timestamp(self):
        s = make_session(self.tmpdir)
        cp = s.save("step", [], "crash")
        self.assertIn("timestamp", cp)
        self.assertTrue(cp["timestamp"].endswith("Z"))

    def test_checkpoint_has_cwd(self):
        s = make_session(self.tmpdir)
        cp = s.save("step", [], "crash")
        self.assertEqual(cp["cwd"], self.tmpdir)


# ── Context injection ─────────────────────────────────────────────────────────

class TestInjectContext(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_file = Path(self.tmpdir) / "session.jsonl"
        # Write a fake existing session file
        existing = {"type": "assistant", "message": {"role": "assistant", "content": "hello"}}
        self.session_file.write_text(json.dumps(existing) + "\n")

    def _make_checkpoint(self, **kwargs):
        defaults = {
            "task_description": "migrate auth.ts",
            "last_step": "read all route files",
            "files_modified": ["auth.ts", "users.ts"],
            "interrupted_by": "rate_limit",
        }
        defaults.update(kwargs)
        return defaults

    def test_inject_appends_to_file(self):
        cp = self._make_checkpoint()
        lines_before = len(self.session_file.read_text().splitlines())
        rt.inject_context(self.session_file, cp)
        lines_after = len(self.session_file.read_text().splitlines())
        self.assertEqual(lines_after, lines_before + 1)

    def test_injected_message_is_valid_json(self):
        rt.inject_context(self.session_file, self._make_checkpoint())
        lines = self.session_file.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        self.assertIn("type", last)
        self.assertIn("message", last)

    def test_injected_message_type_is_user(self):
        rt.inject_context(self.session_file, self._make_checkpoint())
        lines = self.session_file.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        self.assertEqual(last["type"], "user")
        self.assertEqual(last["message"]["role"], "user")

    def test_content_contains_task(self):
        cp = self._make_checkpoint(task_description="special task XYZ")
        rt.inject_context(self.session_file, cp)
        content = self.session_file.read_text()
        self.assertIn("special task XYZ", content)

    def test_content_contains_last_step(self):
        cp = self._make_checkpoint(last_step="migrated auth.ts to JWT")
        rt.inject_context(self.session_file, cp)
        content = self.session_file.read_text()
        self.assertIn("migrated auth.ts to JWT", content)

    def test_content_contains_files(self):
        cp = self._make_checkpoint(files_modified=["auth.ts", "orders.ts"])
        rt.inject_context(self.session_file, cp)
        content = self.session_file.read_text()
        self.assertIn("auth.ts", content)
        self.assertIn("orders.ts", content)

    def test_content_contains_do_not_restart_instruction(self):
        rt.inject_context(self.session_file, self._make_checkpoint())
        content = self.session_file.read_text()
        self.assertIn("DO NOT START OVER", content)

    def test_returns_true_on_success(self):
        result = rt.inject_context(self.session_file, self._make_checkpoint())
        self.assertTrue(result)

    def test_returns_false_for_nonexistent_file(self):
        result = rt.inject_context(Path("/nonexistent/path.jsonl"), self._make_checkpoint())
        self.assertFalse(result)

    def test_returns_false_for_none(self):
        result = rt.inject_context(None, self._make_checkpoint())
        self.assertFalse(result)

    def test_injected_message_has_uuid(self):
        rt.inject_context(self.session_file, self._make_checkpoint())
        lines = self.session_file.read_text().strip().splitlines()
        last = json.loads(lines[-1])
        self.assertIn("uuid", last)
        self.assertIsNotNone(last["uuid"])

    def test_multiple_injections_all_appended(self):
        rt.inject_context(self.session_file, self._make_checkpoint(last_step="step 1"))
        rt.inject_context(self.session_file, self._make_checkpoint(last_step="step 2"))
        lines = self.session_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)  # 1 original + 2 injected


# ── Session file discovery ────────────────────────────────────────────────────

class TestFindClaudeSessionFile(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fake_projects = Path(self.tmpdir) / ".claude" / "projects"
        self.orig_projects = rt.CLAUDE_PROJECTS_DIR
        rt.CLAUDE_PROJECTS_DIR = self.fake_projects

    def tearDown(self):
        rt.CLAUDE_PROJECTS_DIR = self.orig_projects

    def _make_project(self, cwd_path):
        slug = str(cwd_path).replace("/", "-")
        proj_dir = self.fake_projects / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        f = proj_dir / "session-abc.jsonl"
        f.write_text('{"type":"user"}\n')
        return f

    def test_finds_exact_match(self):
        cwd = Path("/some/project/dir")
        expected = self._make_project(cwd)
        result = rt.find_claude_session_file(cwd)
        self.assertEqual(result, expected)

    def test_finds_parent_dir(self):
        parent = Path("/some/project")
        cwd = parent / "src"
        # Only parent has a project dir
        self._make_project(parent)
        result = rt.find_claude_session_file(cwd)
        self.assertIsNotNone(result)

    def test_returns_most_recent_when_multiple(self):
        cwd = Path("/test/project")
        slug = str(cwd).replace("/", "-")
        proj_dir = self.fake_projects / slug
        proj_dir.mkdir(parents=True, exist_ok=True)

        old_f = proj_dir / "old-session.jsonl"
        old_f.write_text('{"type":"user"}\n')
        time.sleep(0.01)
        new_f = proj_dir / "new-session.jsonl"
        new_f.write_text('{"type":"user"}\n')

        result = rt.find_claude_session_file(cwd)
        self.assertEqual(result, new_f)

    def test_fallback_to_recent_any_project(self):
        # No project for current cwd, but a recently modified one exists
        other = Path("/other/project")
        f = self._make_project(other)
        # Touch it to be recent
        f.touch()

        result = rt.find_claude_session_file(Path("/completely/different/dir"))
        self.assertEqual(result, f)

    def test_returns_none_when_no_projects(self):
        result = rt.find_claude_session_file(Path("/nonexistent/dir"))
        self.assertIsNone(result)

    def test_ignores_old_files_in_fallback(self):
        # Create a file older than 30 min — should not be found in fallback
        other = Path("/old/project")
        slug = str(other).replace("/", "-")
        proj_dir = self.fake_projects / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        f = proj_dir / "old.jsonl"
        f.write_text('{"type":"user"}\n')
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(f, (old_time, old_time))

        result = rt.find_claude_session_file(Path("/completely/different"))
        self.assertIsNone(result)


# ── get_modified_files ────────────────────────────────────────────────────────

class TestGetModifiedFiles(unittest.TestCase):

    def test_returns_list(self):
        result = rt.get_modified_files()
        self.assertIsInstance(result, list)

    def test_no_duplicates(self):
        result = rt.get_modified_files()
        self.assertEqual(len(result), len(set(result)))

    def test_sorted(self):
        result = rt.get_modified_files()
        self.assertEqual(result, sorted(result))

    def test_handles_no_git_repo(self):
        # Should not raise even outside a git repo
        orig_dir = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())
            result = rt.get_modified_files()
            self.assertIsInstance(result, list)
        finally:
            os.chdir(orig_dir)


# ── Integration: checkpoint round-trip ───────────────────────────────────────

class TestCheckpointRoundTrip(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        rt.CHECKPOINT_DIR = Path(self.tmpdir) / ".ai-runtime" / "sessions"
        self.session_file = Path(self.tmpdir) / "session.jsonl"
        self.session_file.write_text('{"type":"user","message":{"role":"user","content":"start"}}\n')

    def test_save_then_inject(self):
        s = rt.Session("abc123", "refactor auth", cwd=self.tmpdir)
        cp = s.save("migrated auth.ts", ["auth.ts"], "rate_limit")

        ok = rt.inject_context(self.session_file, cp)
        self.assertTrue(ok)

        lines = self.session_file.read_text().strip().splitlines()
        injected = json.loads(lines[-1])
        content = injected["message"]["content"]

        self.assertIn("refactor auth", content)
        self.assertIn("migrated auth.ts", content)
        self.assertIn("auth.ts", content)

    def test_full_cycle_multiple_interrupts(self):
        s = rt.Session("xyz789", "big migration", cwd=self.tmpdir)

        # Simulate 3 interrupts
        for i in range(1, 4):
            cp = s.save(f"completed step {i}", [f"file{i}.ts"], "rate_limit")
            ok = rt.inject_context(self.session_file, cp)
            self.assertTrue(ok)

        lines = self.session_file.read_text().strip().splitlines()
        # 1 original + 3 injected
        self.assertEqual(len(lines), 4)

        last = json.loads(lines[-1])
        self.assertIn("completed step 3", last["message"]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
