import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from watchdog.claude import transcript_usage, run_hook
from watchdog.config import Config, Paths
from watchdog.core import Core
from watchdog.handoff import Handoffs
from watchdog.install import update_json
from watchdog.server import serve, rpc
from watchdog.store import Store


class FakeHerdr:
    def __init__(self):
        self.live = {"w1:p1": self.agent("w1:p1", "pi", "original")}
        self.started = []
        self.closed = []
        self.prompts = []
        self.fail_prompt = set()
        self.fail_start = False
        self.valid_pids = {101, 202}
        self.verifications = 0

    def agent(self, pane, kind, name):
        return {"pane_id": pane, "terminal_id": "term_" + pane, "agent": kind,
                "name": name, "cwd": "/repo", "agent_status": "working"}

    def agents(self):
        return dict(self.live)

    def verify_process(self, pane, pids):
        self.verifications += 1
        matched = self.valid_pids.intersection(pids)
        if not matched:
            raise ValueError("Caller is not the foreground agent")
        return sorted(matched)

    def split(self, pane, cwd):
        self.split_source = (pane, cwd)
        self.splits = getattr(self, "splits", 1) + 1
        return f"w1:p{self.splits}"

    def start(self, name, kind, pane, args):
        self.started.append((name, kind, pane, args))
        if self.fail_start:
            raise RuntimeError("startup timeout")
        self.live[pane] = self.agent(pane, kind, name)

    def prompt(self, pane, text):
        if pane in self.fail_prompt:
            raise RuntimeError("agent_prompt_stalled")
        self.prompts.append((pane, text))

    def close(self, pane):
        self.closed.append(pane)
        del self.live[pane]

    def run(self, *args):
        return {"panes": list(self.live.values())}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acw-test-")
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "state.sqlite3")
        self.herdr = FakeHerdr()
        self.now = 1000
        self.core = Core(self.store, self.herdr, self.path / "config.json", "/repo/bin/acw", lambda: self.now)
        self.handoffs = Handoffs(self.core, self.path / "handoffs")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def report(self, tokens=200_000, **changes):
        return {"op": "observe", "pane": "w1:p1", "session": "session-1", "kind": "pi",
                "model": "provider/model", "tokens": tokens, "quality": "estimated",
                "pids": [101], "cwd": "/repo", **changes}

    def deliver(self, response):
        self.core.delivered({"pane": "w1:p1", "key": response["key"], "id": response["notification"]["id"]})

    def start(self):
        self.core.observe(self.report())
        document = self.path / "handoff.md"
        document.write_text("# Task\nFinish the tests.\n")
        return self.handoffs.start({"pane": "w1:p1", "pids": [999], "session": "session-1",
                                    "document": str(document), "quiesced": True})

    def acknowledge(self, handoff):
        self.core.observe(self.report(1000, pane="w1:p2", session="session-2", pids=[202]))
        return self.handoffs.ready({"id": handoff["id"], "pane": "w1:p2", "pids": [999],
                                   "confirm_read": True, "sha256": handoff["sha256"]})

    def test_global_and_exact_model_precedence(self):
        config = Config(model_limits={"claude-haiku-5-5": 100000, "special/claude-haiku-5-5": 150000})
        self.assertEqual(config.limit("any/model"), (200000, "global"))
        self.assertEqual(config.limit("anthropic/claude-haiku-5-5")[0], 100000)
        self.assertEqual(config.limit("special/claude-haiku-5-5")[0], 150000)
        self.assertEqual(config.limit("claude-haiku-4-5")[0], 200000)

    def test_haiku_threshold(self):
        self.assertIsNone(self.core.observe(self.report(99999, model="anthropic/claude-haiku-5-5"))["notification"])
        self.assertIsNotNone(self.core.observe(self.report(100000, model="anthropic/claude-haiku-5-5"))["notification"])

    def test_config_rejects_resuming_successors(self):
        for flag in ("--continue", "--resume=old", "--session", "--standalone"):
            (self.path / "config.json").write_text(json.dumps({"launch_args": {"pi": [flag]}}))
            with self.assertRaisesRegex(ValueError, "fresh-session"):
                Config.load(self.path / "config.json")

    def test_growth_reminders_and_compaction(self):
        first = self.core.observe(self.report())
        self.assertFalse(first["notification"]["repeat"])
        self.deliver(first)
        self.assertIsNone(self.core.observe(self.report(219999))["notification"])
        second = self.core.observe(self.report(220000))
        self.assertTrue(second["notification"]["repeat"])
        self.deliver(second)
        self.core.observe(self.report(50000))
        self.assertIsNotNone(self.core.observe(self.report())["notification"])

    def test_parallel_reports_lease_one_delivery(self):
        first = self.core.observe(self.report())
        second = self.core.observe(self.report())
        self.assertIsNotNone(first["notification"])
        self.assertIsNone(second["notification"])
        self.now += 16
        retry = self.core.observe(self.report())
        self.assertEqual(retry["notification"]["id"], first["notification"]["id"])

    def test_restart_retains_deduplication(self):
        self.deliver(self.core.observe(self.report()))
        self.store.close()
        self.store = Store(self.path / "state.sqlite3")
        self.core = Core(self.store, self.herdr, self.path / "config.json", "acw", lambda: self.now)
        self.assertIsNone(self.core.observe(self.report())["notification"])

    def test_unknown_and_lower_usage_clear_pending(self):
        self.core.observe(self.report())
        self.now += 16
        self.assertIsNone(self.core.observe(self.report(None))["notification"])
        self.assertEqual(self.core.status()["agents"][0]["telemetry"], "unknown")
        self.core.observe(self.report())
        self.now += 16
        self.assertIsNone(self.core.observe(self.report(195000))["notification"])

    def test_model_switch_and_live_config_reload(self):
        self.core.observe(self.report(110000))
        self.assertIsNotNone(self.core.observe(self.report(110000, model="claude-haiku-5-5"))["notification"])
        (self.path / "config.json").write_text(json.dumps({"global_limit": 90000}))
        self.assertIsNotNone(self.core.observe(self.report(95000))["notification"])
        (self.path / "config.json").write_text('{"global_limit": -10}')
        self.assertIsNotNone(self.core.status()["config_error"])
        self.assertEqual(self.core.config.global_limit, 90000)

    def test_missing_stale_and_outside_agents(self):
        self.assertEqual(self.core.status()["agents"][0]["telemetry"], "missing")
        self.assertIn("ignored", self.core.observe(self.report(pane="w9:p9")))
        self.core.observe(self.report())
        self.now += 121
        self.assertEqual(self.core.status()["agents"][0]["telemetry"], "stale")

    def test_named_pane_without_agent_kind_is_ignored(self):
        # Herdr keeps a name on a pane whose agent exited, with no agent label.
        self.herdr.live["w1:p8"] = {"pane_id": "w1:p8", "terminal_id": "term_w1:p8",
                                    "name": "leftover", "agent_status": "unknown"}
        self.assertEqual([row["pane"] for row in self.core.status()["agents"]], ["w1:p1"])
        self.assertIn("ignored", self.core.observe(self.report(pane="w1:p8")))

    def test_unknown_process_cannot_register(self):
        with self.assertRaisesRegex(ValueError, "foreground"):
            self.core.observe(self.report(pids=[999]))

    def test_hook_reports_from_validated_agent_skip_revalidation(self):
        # Each Claude hook is a new process beneath the same foreground harness.
        for hook in (5001, 5002, 5003):
            self.core.observe(self.report(pids=[hook, 101]))
        self.assertEqual(self.herdr.verifications, 1)
        with self.assertRaisesRegex(ValueError, "foreground"):
            self.core.observe(self.report(pids=[5004, 999]))

    def test_handoff_waits_for_explicit_ack_and_preserves_model(self):
        handoff = self.start()
        self.assertEqual(handoff["state"], "awaiting_ack")
        self.assertEqual(self.herdr.split_source, ("w1:p1", "/repo"))
        self.assertEqual(self.herdr.started[0][3], ["--model", "provider/model"])
        request = {"id": handoff["id"], "pane": "w1:p1", "session": "session-1"}
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.handoffs.retire(request)
        self.assertEqual(self.herdr.closed, [])
        self.acknowledge(handoff)
        self.assertTrue(self.handoffs.retire(request)["retired"])
        self.assertEqual(self.herdr.closed, ["w1:p1"])

    def test_startup_failure_and_retry_do_not_spawn_twice(self):
        self.herdr.fail_start = True
        first = self.start()
        second = self.start()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "failed")
        self.assertEqual(len(self.herdr.started), 1)
        self.assertEqual(self.herdr.closed, [])

    def test_wrong_successor_or_changed_document_cannot_ack(self):
        handoff = self.start()
        with self.assertRaisesRegex(ValueError, "designated"):
            self.handoffs.ready({"id": handoff["id"], "pane": "w1:p1", "confirm_read": True})
        Path(handoff["document"]).write_text("Modified after delivery")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.acknowledge(handoff)
        self.assertEqual(self.herdr.closed, [])

    def test_original_cannot_retire_after_successor_disappears(self):
        handoff = self.start()
        self.acknowledge(handoff)
        del self.herdr.live["w1:p2"]
        with self.assertRaisesRegex(ValueError, "no longer running"):
            self.handoffs.retire({"id": handoff["id"], "pane": "w1:p1", "session": "session-1"})

    def test_original_can_retire_after_its_successor_hands_off_in_turn(self):
        first = self.start()
        self.acknowledge(first)
        self.core.observe(self.report(pane="w1:p2", session="session-2", pids=[202]))
        document = self.path / "second.md"
        document.write_text("Next steps")
        second = self.handoffs.start({"pane": "w1:p2", "session": "session-2",
                                      "document": str(document), "quiesced": True})
        self.assertEqual(second["successor_pane"], "w1:p3")
        self.core.observe(self.report(1000, pane="w1:p3", session="session-3", pids=[202]))
        self.handoffs.ready({"id": second["id"], "pane": "w1:p3", "confirm_read": True, "sha256": second["sha256"]})
        self.handoffs.retire({"id": second["id"], "pane": "w1:p2", "session": "session-2"})
        self.assertTrue(self.handoffs.retire({"id": first["id"], "pane": "w1:p1", "session": "session-1"})["retired"])
        self.assertEqual(self.herdr.closed, ["w1:p2", "w1:p1"])

    def test_original_cannot_retire_if_it_switched_sessions(self):
        handoff = self.start()
        self.acknowledge(handoff)
        self.core.observe(self.report(1000, session="another-session"))
        with self.assertRaisesRegex(ValueError, "active telemetry"):
            self.handoffs.retire({"id": handoff["id"], "pane": "w1:p1", "session": "session-1"})

    def test_open_tab_guard(self):
        self.core.observe(self.report(can_retire=False))
        doc = self.path / "handoff.md"
        doc.write_text("Work remaining")
        with self.assertRaisesRegex(ValueError, "tabs"):
            self.handoffs.start({"pane": "w1:p1", "session": "session-1", "document": str(doc), "quiesced": True})

    def test_handoff_suppresses_further_reminders(self):
        self.start()
        self.now += 16
        self.assertIsNone(self.core.observe(self.report(250000))["notification"])

    def test_failed_handoff_resumes_reminders_and_explains_cancel(self):
        self.deliver(self.core.observe(self.report()))
        self.herdr.fail_start = True
        handoff = self.start()
        self.assertEqual(handoff["state"], "failed")
        reminder = self.core.observe(self.report(220000))["notification"]
        self.assertIn(f"handoff cancel {handoff['id']} --session session-1", reminder["text"])

    def original_prompts(self):
        return [text for pane, text in self.herdr.prompts if pane == "w1:p1"]

    def test_idle_original_is_prompted_once_to_retire_after_ack(self):
        handoff = self.start()
        self.herdr.live["w1:p1"]["agent_status"] = "idle"
        self.handoffs.prompt_retirements()
        self.assertEqual(self.original_prompts(), [])
        self.acknowledge(handoff)
        self.handoffs.prompt_retirements()
        self.handoffs.prompt_retirements()
        [text] = self.original_prompts()
        self.assertIn(f"/repo/bin/acw --pane w1:p1 handoff retire {handoff['id']} --session session-1", text)
        self.assertEqual(self.handoffs.get(handoff["id"])["retire_prompted_at"], self.now)

    def test_working_or_replaced_original_is_not_prompted(self):
        handoff = self.start()
        self.acknowledge(handoff)
        self.handoffs.prompt_retirements()
        self.assertEqual(self.original_prompts(), [])
        self.herdr.live["w1:p1"].update(agent_status="done", terminal_id="term_other")
        self.handoffs.prompt_retirements()
        self.assertEqual(self.original_prompts(), [])
        self.herdr.live["w1:p1"]["terminal_id"] = "term_w1:p1"
        self.handoffs.prompt_retirements()
        self.assertEqual(len(self.original_prompts()), 1)

    def test_failed_retire_prompt_is_retried_later(self):
        handoff = self.start()
        self.acknowledge(handoff)
        self.herdr.live["w1:p1"]["agent_status"] = "idle"
        self.herdr.fail_prompt.add("w1:p1")
        self.handoffs.prompt_retirements()
        self.assertIn("stalled", self.handoffs.get(handoff["id"])["retire_prompt_error"])
        self.herdr.fail_prompt.clear()
        self.handoffs.prompt_retirements()
        self.assertEqual(self.original_prompts(), [])
        self.now += 31
        self.handoffs.prompt_retirements()
        self.assertEqual(len(self.original_prompts()), 1)
        self.assertIsNone(self.handoffs.get(handoff["id"])["retire_prompt_error"])

    def test_successor_monitored_normally(self):
        handoff = self.start()
        self.acknowledge(handoff)
        result = self.core.observe(self.report(pane="w1:p2", session="session-2", pids=[202]))
        self.assertIsNotNone(result["notification"])

    def test_opencode_preparation_handshake_before_start(self):
        self.herdr.live["w1:p1"]["agent"] = "opencode"
        self.core.observe(self.report(kind="opencode", profile="build"))
        doc = self.path / "handoff.md"
        doc.write_text("Continue work")
        result = []
        thread = threading.Thread(target=lambda: result.append(self.handoffs.start({
            "pane": "w1:p1", "session": "session-1", "document": str(doc), "quiesced": True,
        })))
        thread.start()
        deadline = time.monotonic() + 2
        preparation = None
        while time.monotonic() < deadline and not preparation:
            preparation = self.core.observe(self.report(kind="opencode"))["prepare"]
            time.sleep(0.005)
        self.assertIsNotNone(preparation)
        self.assertEqual(self.herdr.started, [])
        self.core.prepared({"id": preparation["id"], "pane": "w1:p1"})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.herdr.started[0][3], ["--session", preparation["session"]])
        self.assertEqual(result[0]["state"], "awaiting_ack")


class AdapterTests(unittest.TestCase):
    def test_claude_latest_request_not_lifetime_and_cache_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.jsonl"
            def assistant(n):
                return {"type": "assistant", "message": {"model": "claude-haiku-5-5", "usage": {
                    "input_tokens": n, "output_tokens": 20, "cache_read_input_tokens": 90000,
                    "cache_creation_input_tokens": 10000}}}
            path.write_text("\n".join(json.dumps(x) for x in [assistant(500000), assistant(100)]))
            self.assertEqual(transcript_usage(path), ("claude-haiku-5-5", 100120))
            with path.open("a") as stream:
                stream.write('\n{"type":"system","subtype":"compact_boundary"}\n')
            self.assertEqual(transcript_usage(path)[1], None)
            with path.open("a") as stream:
                stream.write(json.dumps(assistant(50)) + '\n{"partial":')
            self.assertEqual(transcript_usage(path)[1], 100070)

    def test_claude_hook_injects_and_confirms_delivery(self):
        responses = [{"key": "key", "notification": {"id": "n", "text": "Consider a handoff"}}, {}]
        output = io.StringIO()
        with patch("watchdog.claude.transcript_usage", return_value=("haiku", 100000)), \
             patch("watchdog.claude.caller", return_value={"pane": "w1:p1"}), \
             patch("watchdog.claude.rpc", side_effect=responses) as mock, contextlib.redirect_stdout(output):
            run_hook(None, {"hook_event_name": "PostToolUse", "session_id": "s", "transcript_path": "x", "cwd": "/repo"})
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"], "Consider a handoff")
        self.assertEqual(mock.call_args.args[1]["op"], "delivered")

    def run_stop_hook(self, notification):
        output = io.StringIO()
        with patch("watchdog.claude.transcript_usage", return_value=("haiku", 130000)), \
             patch("watchdog.claude.caller", return_value={"pane": "w1:p1"}), \
             patch("watchdog.claude.rpc", side_effect=[{"key": "key", "notification": notification}, {}]) as mock, \
             contextlib.redirect_stdout(output):
            run_hook(None, {"hook_event_name": "Stop", "session_id": "s", "transcript_path": "x", "cwd": "/repo"})
        return output.getvalue(), [call.args[1]["op"] for call in mock.call_args_list]

    def test_claude_stop_blocks_for_first_reminder_but_holds_repeats(self):
        output, ops = self.run_stop_hook({"id": "n", "text": "Consider a handoff", "repeat": False})
        self.assertEqual(json.loads(output), {"decision": "block", "reason": "Consider a handoff"})
        self.assertEqual(ops, ["observe", "delivered"])
        output, ops = self.run_stop_hook({"id": "n", "text": "Consider a handoff", "repeat": True})
        self.assertEqual(output, "")
        self.assertEqual(ops, ["observe"])

    def test_claude_hook_registers_before_transcript_exists(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("watchdog.claude.caller", return_value={"pane": "w1:p1"}), \
             patch("watchdog.claude.rpc", return_value={"key": "key"}) as mock:
            run_hook(None, {"hook_event_name": "SessionStart", "session_id": "s", "model": "claude-haiku-5-5",
                            "transcript_path": str(Path(tmp) / "missing.jsonl"), "cwd": "/repo"})
        request = mock.call_args.args[1]
        self.assertEqual((request["op"], request["model"], request["tokens"]), ("observe", "claude-haiku-5-5", None))

    def test_claude_subagents_are_not_the_pane_owner(self):
        with patch("watchdog.claude.rpc") as mock:
            run_hook(None, {"agent_id": "child"})
        mock.assert_not_called()

    def test_install_preserves_unrelated_settings_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"permissions": {"mode": "ask"}, "hooks": {"Stop": []}}')
            update_json(path, lambda d: d["hooks"]["Stop"].append({"test": True}))
            self.assertEqual(json.loads(path.read_text())["permissions"], {"mode": "ask"})
            self.assertEqual(len(list(Path(tmp).glob("*.acw-backup-*"))), 1)


class ServerTests(unittest.TestCase):
    def test_real_socket_round_trip_restart_and_config_reload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/fake/herdr.sock", "HERDR_PANE_ID": "w1:p1",
            "ACW_HOME": tmp, "XDG_RUNTIME_DIR": tmp,
        }):
            paths = Paths()
            for iteration in range(2):
                stop = threading.Event()
                thread = threading.Thread(target=serve, args=(paths, "/repo/bin/acw", FakeHerdr(), stop))
                thread.start()
                deadline = time.monotonic() + 3
                while not paths.socket.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                try:
                    response = rpc(paths, {"op": "observe", "pane": "w1:p1", "pids": [101],
                                           "kind": "pi", "session": "s", "model": "model", "tokens": 200000,
                                           "quality": "estimated"})
                    if iteration == 0:
                        self.assertIsNotNone(response["notification"])
                        rpc(paths, {"op": "delivered", "pane": "w1:p1", "key": response["key"],
                                    "id": response["notification"]["id"]})
                    else:
                        self.assertIsNone(response["notification"])
                    self.assertEqual(rpc(paths, {"op": "status"})["agents"][0]["tokens"], 200000)
                    rpc(paths, {"op": "stop"})
                finally:
                    stop.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(paths.socket.exists())


if __name__ == "__main__":
    unittest.main()
