"""透過實際子程序與 pipe 驗證 issue #2 的下載交接。

只替換 Popen 的啟動命令，runner、協定解析、逾時、取消與 EPUB 驗證照常執行。
使用本地 Python 假核心，不連網、不建立 GUI，也不使用既有下載目錄。
設定 ISSUE2_TEST_ARTIFACTS 可保留逐案證據，否則使用獨立暫存目錄。
"""
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
import packer

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_packer.py"
REAL_POPEN = subprocess.Popen


def read_events(directory):
    path = directory / "fixture-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line.decode("utf-8")) for line in path.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n")]


class PackerRunnerRegression(unittest.TestCase):
    def setUp(self):
        evidence = os.environ.get("ISSUE2_TEST_ARTIFACTS")
        if evidence:
            self.directory = Path(evidence).resolve() / (self._testMethodName + "-" + uuid.uuid4().hex[:8])
            self.directory.mkdir(parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="issue2-packer-")
            self.addCleanup(temporary.cleanup)
            self.directory = Path(temporary.name)

    def run_fixture(self, scenario="single", merge="2", title="1", *, fragment=False,
                    cancel_when_started=False, timeout_seconds=4, stall_seconds=3, legacy=False,
                    version=None, unknown_prompt=False, unknown_boundary=False):
        commands, processes, process_callbacks, lines = [], [], [], []
        cancel = threading.Event()
        done = threading.Event()
        watchdog_fired = threading.Event()
        cancellation_sent = threading.Event()

        def spawn(_command, *args, **kwargs):
            command = [sys.executable, "-B", "-u", str(FIXTURE), "--scenario", scenario,
                       "--between-volumes", "0.3"]
            if fragment:
                command.append("--fragment-prompts")
            if legacy:
                command.append('--legacy')
            if version:
                command.extend(['--version', version])
            if unknown_prompt:
                command.append('--unknown-prompt')
            if unknown_boundary:
                command.append('--unknown-boundary')
            commands.append(command)
            proc = REAL_POPEN(command, *args, **kwargs)
            processes.append(proc)
            return proc

        def monitor():
            started = time.monotonic()
            while not done.wait(0.01):
                if cancel_when_started and not cancellation_sent.is_set():
                    if any(event["event"] == "job_started" for event in read_events(self.directory)):
                        cancellation_sent.set()
                        cancel.set()
                if time.monotonic() - started > 10:
                    watchdog_fired.set()
                    cancel.set()
                    for proc in processes:
                        if proc.poll() is None:
                            proc.kill()
                    return

        guard = threading.Thread(target=monitor, daemon=True)
        guard.start()
        before = time.monotonic()
        try:
            with patch.object(packer.subprocess, "Popen", side_effect=spawn):
                result = packer.run_interactive_packer(
                    self.directory / "bili_novel_packer-0.2.50-x86_64-windows.exe",
                    "https://fixture.invalid/novel/1", "0", merge, title,
                    self.directory, cancel,
                    on_process=process_callbacks.append, on_line=lines.append,
                    timeout_seconds=timeout_seconds, stall_seconds=stall_seconds,
                )
            elapsed = time.monotonic() - before
            alive_at_return = [proc.pid for proc in processes if proc.poll() is None]
        finally:
            done.set()
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=3)
            guard.join(timeout=1)
        events = read_events(self.directory)
        payload = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else vars(result)
        evidence = {
            "result": payload, "elapsed_seconds": elapsed, "scenario": scenario,
            "requested_merge": merge, "requested_title": title, "fragmented": fragment,
            "child_commands": commands, "child_exit_codes": [proc.returncode for proc in processes],
            "children_alive_at_return": alive_at_return, "watchdog_fired": watchdog_fired.is_set(),
            "cancellation_sent": cancellation_sent.is_set(),
            "events": events, "on_line": lines, "python": sys.executable,
        }
        (self.directory / "regression-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.assertFalse(watchdog_fired.is_set(), "runner exceeded independent 10-second watchdog")
        self.assertFalse(alive_at_return, "runner returned while child was still alive")
        self.assertEqual(len(processes), 1)
        self.assertEqual(process_callbacks, processes)
        self.assertFalse(guard.is_alive())
        self.assertLess(elapsed, 6)
        return result, events, elapsed

    def assert_success(self, result, events, count, title):
        self.assertTrue(result.success, result)
        self.assertEqual(len(result.output_files), count, result)
        self.assertEqual(sum(event["event"] == "task_boundary" for event in events), 1)
        self.assertEqual(sum(event["event"] == "job_started" for event in events), 1)
        self.assertFalse(any(event["event"] == "eof" for event in events), "runner closed stdin before stopping idle core")
        self.assertEqual(sum(event["event"] == "input" and event["kind"] == "url" for event in events), 1)
        observed = next(event for event in events if event["event"] == "job_started")
        self.assertEqual(observed["title"], title)
        for item in result.output_files:
            path = Path(item)
            self.assertTrue(path.is_file(), path)
            self.assertIn(self.directory.resolve(), path.resolve().parents)
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read("mimetype"), b"application/epub+zip")

    def test_single_title_on_skips_gui_merge_off_answer(self):
        result, events, _ = self.run_fixture(merge="2", title="1")
        self.assert_success(result, events, 1, "1")
        self.assertFalse(any(e["event"] == "input" and e["kind"] == "merge" for e in events))

    def test_single_title_off_skips_gui_merge_on_answer(self):
        result, events, _ = self.run_fixture(merge="1", title="2")
        self.assert_success(result, events, 1, "2")

    def test_fragmented_utf8_prompts_without_newline(self):
        result, events, _ = self.run_fixture(fragment=True)
        self.assert_success(result, events, 1, "1")
        self.assertTrue(events[0]["fragmented"])

    def test_multi_volume_waits_for_final_boundary(self):
        result, events, _ = self.run_fixture("multi", merge="2", title="1")
        self.assert_success(result, events, 2, "1")
        closed = [e for e in events if e["event"] == "epub_closed"]
        self.assertEqual([e["volumes"] for e in closed], [[1], [2]])
        boundary = next(e for e in events if e["event"] == "task_boundary")
        self.assertLess(closed[0]["monotonic"], closed[1]["monotonic"])
        # Windows 相鄰事件可能共用同一個 monotonic 時鐘刻度，另核對事件順序
        self.assertLess(events.index(closed[1]), events.index(boundary))

    def test_multi_volume_merge_produces_one_output(self):
        result, events, _ = self.run_fixture("multi", merge="1", title="2")
        self.assert_success(result, events, 1, "2")
        output = next(e for e in events if e["event"] == "epub_closed")
        self.assertEqual(output["volumes"], [1, 2])

    def test_error_returning_to_prompt_is_not_success(self):
        result, events, _ = self.run_fixture("error", merge="2")
        self.assertFalse(result.success, result)
        self.assertTrue(any(e["event"] == "fatal_error" for e in events))
        self.assertEqual(len(list(self.directory.glob("FixtureBook/*.epub"))), 1,
                         "partial output must remain available for diagnosis")
        self.assertNotIn(result.reason, ("timeout", "stall", "cancelled"), result)

    def test_cancel_running_child_has_bounded_cleanup(self):
        result, events, _ = self.run_fixture("hang", cancel_when_started=True)
        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "cancelled")
        self.assertTrue(any(e["event"] == "job_started" for e in events))

    def test_total_deadline_stops_child(self):
        result, _, _ = self.run_fixture("hang", timeout_seconds=0.5, stall_seconds=3)
        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "timeout")

    def test_stall_deadline_stops_child(self):
        result, _, _ = self.run_fixture("hang", timeout_seconds=4, stall_seconds=0.4)
        self.assertFalse(result.success, result)
        self.assertEqual(result.reason, "stall")

    def test_bad_zip_rejected_even_with_completion_marker(self):
        result, events, _ = self.run_fixture("invalid")
        self.assertFalse(result.success, result)
        self.assertTrue(any(e["event"] == "epub_closed" and e.get("invalid") for e in events))
        self.assertNotIn(result.reason, ("timeout", "stall", "cancelled"), result)
        self.assertEqual(len(list(self.directory.glob("FixtureBook/*.epub"))), 1)

    def test_zero_outputs_rejected_at_idle_boundary(self):
        result, events, _ = self.run_fixture("zero")
        self.assertFalse(result.success, result)
        self.assertTrue(any(e["event"] == "task_boundary" for e in events))
        self.assertNotIn(result.reason, ("timeout", "stall", "cancelled"), result)
        self.assertFalse(list(self.directory.glob("FixtureBook/*.epub")))

    def test_legacy_completion_sends_enter_and_exits_naturally(self):
        result, events, _ = self.run_fixture(legacy=True)
        self.assert_success(result, events, 1, '1')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(next(e for e in events if e['event']=='legacy_final_enter')['value'], '\n')

    def test_legacy_error_is_failure_despite_partial_valid_output(self):
        result, events, _ = self.run_fixture('error', legacy=True)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'error')
        self.assertEqual(len(list(self.directory.glob('FixtureBook/*.epub'))), 1)

    def test_native_log_keeps_silent_stdout_job_alive(self):
        result, events, elapsed = self.run_fixture('quietlog', stall_seconds=.35)
        self.assert_success(result, events, 1, '1')
        self.assertGreater(elapsed, .9)

    def test_large_stderr_drained_and_console_file_bounded(self):
        result, events, _ = self.run_fixture('flood')
        self.assert_success(result, events, 1, '1')
        self.assertLessEqual((self.directory/'packer_console.log').stat().st_size, 2*1024*1024)

    def test_unknown_current_version_single_fragmented(self):
        with self.assertLogs(level='WARNING') as logs:
            result, events, _ = self.run_fixture(version='9.9.9', fragment=True)
        self.assert_success(result, events, 1, '1')
        self.assertTrue(any('尚未實測' in line for line in logs.output))

    def test_unknown_legacy_version_exits_naturally(self):
        result, events, _ = self.run_fixture(version='9.9.9', legacy=True)
        self.assert_success(result, events, 1, '1')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(next(e for e in events if e['event'] == 'legacy_final_enter')['value'], '\n')

    def test_unknown_version_multi_volume_separate_and_merged(self):
        base = self.directory
        for merge, count in [('2', 2), ('1', 1)]:
            with self.subTest(merge=merge):
                # 每次使用獨立目錄，避免事件與產物混入下一個案例
                self.directory = base / merge
                self.directory.mkdir()
                result, events, _ = self.run_fixture('multi', merge=merge, version='9.9.9')
                self.assert_success(result, events, count, '1')

    def test_unknown_version_partial_failure_is_not_success(self):
        result, events, _ = self.run_fixture('error', version='9.9.9')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'error')
        self.assertTrue(any(e['event'] == 'fatal_error' for e in events))

    def test_unknown_version_invalid_epub_is_rejected(self):
        result, _, _ = self.run_fixture('invalid', version='9.9.9')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'error')

    def test_unknown_version_zero_outputs_is_rejected(self):
        result, _, _ = self.run_fixture('zero', version='9.9.9')
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'error')

    def test_unknown_prompt_stops_without_guessing(self):
        result, events, _ = self.run_fixture(version='9.9.9', unknown_prompt=True)
        self.assertFalse(result.success)
        self.assertIn('未知互動提示', result.message)
        self.assertFalse(any(e['event'] in ('job_started', 'unknown_prompt_reply') for e in events))

    def test_unknown_completion_exit_zero_is_not_success(self):
        result, _, _ = self.run_fixture(version='9.9.9', unknown_boundary=True)
        self.assertFalse(result.success)
        self.assertIn('沒有完整任務結束訊號', result.message)

    def test_unknown_version_cancel_still_cleans_up(self):
        result, _, _ = self.run_fixture('hang', version='9.9.9', cancel_when_started=True)
        self.assertEqual(result.reason, 'cancelled')


if __name__ == "__main__":
    unittest.main(verbosity=2)
