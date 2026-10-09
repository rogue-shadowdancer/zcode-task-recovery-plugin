#!/usr/bin/env python3
"""Unit tests for continuity.py against a temporary state root.

Run: C:\\Windows\\py.exe -3 tests\\test_continuity.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN_ROOT / "hooks" / "continuity.py"
PY = sys.executable if sys.version_info >= (3, 8) else "C:\\Windows\\py.exe"


def run(args, state_root, stdin=None):
    result = subprocess.run(
        [PY, str(SCRIPT), "--zcode-home", str(state_root)] + args,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result


def hook(state_root, payload):
    result = run(["hook"], state_root, stdin=json.dumps(payload))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_lifecycle_and_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # A normal turn opens and closes cleanly.
        assert hook(root, {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup"}) is None
        assert hook(root, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}) is None
        assert hook(root, {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash"}) is None
        assert hook(root, {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash"}) is None
        assert hook(root, {"hook_event_name": "Stop", "session_id": "s1"}) is None

        result = run(["status"], root)
        report = json.loads(result.stdout)
        assert report["sessionCount"] == 1
        assert report["pendingSessions"]["interrupted"] == []

        # Simulate an interrupted foreign-runtime session by seeding state directly.
        sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))
        import continuity

        state = continuity.State(root / continuity.STATE_RELATIVE)
        stale = state.load_session("s-dead")
        stale.update(openTurn=True, runtimeInstanceId="a" * 64, promptCount=4, lastToolName="Edit")
        state.save_session(stale)

        # A fresh startup in a different runtime must acquire a provisional lease
        # and inject additionalContext. Patch the probe for determinism.
        original_probe = continuity.probe_runtime_identity
        continuity.probe_runtime_identity = lambda: ("b" * 64, "ok")
        try:
            output = continuity.handle_hook(
                state, {"hook_event_name": "SessionStart", "session_id": "s2", "source": "startup"}
            )[0]
        finally:
            continuity.probe_runtime_identity = original_probe
        assert output and "additionalContext" in output
        assert "s-dead" in output["additionalContext"]
        lease = state.load_lease()
        assert lease and lease["leaseKind"] == "provisional" and lease["ownerSessionId"] == "s2"

        # Same-runtime startup must NOT steal or duplicate the lease.
        out2 = continuity.handle_hook(
            state, {"hook_event_name": "SessionStart", "session_id": "s3", "source": "startup"}
        )[0]
        assert out2 is None
        assert state.load_lease()["ownerSessionId"] == "s2"

        # Promote, commit a manifest, verify, release.
        result = run(["lease", "renew", "--owner", "s2", "--checkpoint-id", "ck-1", "--lease-kind", "coordinator", "--ttl-seconds", "900"], root)
        assert json.loads(result.stdout)["ok"] is True
        manifest = {
            "schemaVersion": "zcode-recovery-manifest.v1",
            "checkpointId": "ck-1",
            "mode": "unexpected",
            "stage": "recovering",
            "createdAt": "2026-10-09T00:00:00Z",
            "tasks": [],
        }
        result = run(["manifest", "commit"], root, stdin=json.dumps(manifest))
        assert json.loads(result.stdout)["ok"] is True
        result = run(["manifest", "verify", "--latest"], root)
        assert json.loads(result.stdout)["ok"] is True
        # Tampering must break verification.
        path = root / "task-continuity" / "v1" / "manifests" / "ck-1.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"stage": "recovering"', '"stage": "complete"'), encoding="utf-8")
        result = run(["manifest", "verify", "--latest"], root)
        assert json.loads(result.stdout)["ok"] is False
        result = run(["lease", "release", "--owner", "s2"], root)
        assert json.loads(result.stdout)["ok"] is True

        # Never stores sensitive material: session files contain no free text beyond tool names.
        session_files = list((root / "task-continuity" / "v1" / "sessions").glob("*.json"))
        for sf in session_files:
            body = sf.read_text(encoding="utf-8")
            for banned in ("command", "prompt text", "authorization", "token"):
                assert banned not in body.lower() or banned == "command", body

    print("test_lifecycle_and_recovery: passed")


def test_fail_open_and_tolerant_input():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Invalid JSON stdin must fail open (exit 0, no output).
        result = subprocess.run(
            [PY, str(SCRIPT), "--zcode-home", str(root), "hook"],
            input="{not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0 and result.stdout.strip() == ""
        # Key-spelling variants must be understood.
        out = hook(root, {"event_name": "UserPromptSubmit", "sessionId": "s9"})
        assert out is None
        out = hook(root, {"eventName": "SomeFutureEvent", "sessionId": "s9"})
        assert out is None  # unknown events are ignored
    print("test_fail_open_and_tolerant_input: passed")


def test_diagnostics_window():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        result = run(["diagnostics", "arm", "--ttl-minutes", "5"], root)
        assert json.loads(result.stdout)["armed"] is True
        hook(root, {"hook_event_name": "UserPromptSubmit", "session_id": "sd"})
        result = run(["diagnostics", "status"], root)
        status = json.loads(result.stdout)
        assert status["armed"] is True and status["eventCount"] >= 1
        assert status["recent"][-1]["stage"] == "returned"
        result = run(["diagnostics", "disarm"], root)
        assert json.loads(result.stdout)["armed"] is False
    print("test_diagnostics_window: passed")


if __name__ == "__main__":
    test_lifecycle_and_recovery()
    test_fail_open_and_tolerant_input()
    test_diagnostics_window()
    print("all tests passed")
