#!/usr/bin/env python3
"""ZCode task continuity: lifecycle checkpointing, recovery lease, and manifests.

GLM/ZCode adaptation of the recover-codex-tasks continuity design.
Safety invariants (unchanged from the original contract):

- Never store prompt text, command text, tool inputs/outputs, replies,
  credentials, tokens, or environment values. Only counts, tool names,
  timestamps, session/turn hashes, and bounded metadata are persisted.
- Never parse or modify ZCode databases (db.sqlite, tasks-index.sqlite).
  Transcript/rollout files are an unstable read-only last resort.
- Hooks must fail open: an internal error never blocks a session; emit
  nothing, exit 0.
- State lives under <zcode-home>/task-continuity/v1/ as atomic UTF-8 JSON.

Hook input is tolerant of key spellings (session_id/sessionId,
hook_event_name/event_name, source/sessionSource) because the ZCode hook
payload schema is not frozen; unknown events are recorded as "other".
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_SESSION = "zcode-continuity-session.v1"
SCHEMA_LEASE = "zcode-recovery-lease.v1"
SCHEMA_MANIFEST = "zcode-recovery-manifest.v1"
SCHEMA_DIAGNOSTIC = "zcode-continuity-diagnostic.v1"

MANAGED_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Stop",
}
AUTOMATIC_STAGES = {"pending-restart", "recovering"}
MANIFEST_STAGES = {"preparing", "pending-restart", "recovering", "needs-attention", "complete"}
SESSION_LOOKBACK_DAYS = 14
PROVISIONAL_TTL_SECONDS = 90
DEFAULT_COORDINATOR_TTL_SECONDS = 900
MAX_OPEN_TOOLS = 64
DIAGNOSTICS_RETENTION = 200
DIAGNOSTICS_MAX_AGE_DAYS = 7
STATE_RELATIVE = Path("task-continuity") / "v1"

RECOVERY_CONTEXT_LIMIT_CHARS = 1500


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class State:
    def __init__(self, root: Path):
        self.root = root
        self.sessions = root / "sessions"
        self.leases = root / "leases"
        self.manifests = root / "manifests"
        self.diagnostics = root / "diagnostics"
        for directory in (self.sessions, self.leases, self.manifests, self.diagnostics / "events"):
            directory.mkdir(parents=True, exist_ok=True)

    # ---- atomic json helpers -------------------------------------------------

    def write_json(self, path: Path, payload: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def read_json(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- sessions -------------------------------------------------------------

    def session_path(self, session_id: str) -> Path:
        return self.sessions / (sha256_text(session_id)[:24] + ".json")

    def load_session(self, session_id: str) -> dict:
        record = self.read_json(self.session_path(session_id))
        if record and record.get("schemaVersion") == SCHEMA_SESSION:
            return record
        return {
            "schemaVersion": SCHEMA_SESSION,
            "sessionId": session_id,
            "createdAt": utcnow(),
            "updatedAt": utcnow(),
            "lastSource": None,
            "runtimeInstanceId": None,
            "runtimeProbeStatus": "unavailable",
            "promptCount": 0,
            "toolCount": 0,
            "toolResultUnknownCount": 0,
            "permCount": 0,
            "lastToolName": None,
            "openTools": [],
            "lastPromptAt": None,
            "lastStopAt": None,
            "openTurn": False,
            "lastEvent": None,
        }

    def save_session(self, record: dict) -> None:
        record["updatedAt"] = utcnow()
        self.write_json(self.session_path(record["sessionId"]), record)

    def iter_sessions(self) -> list[dict]:
        records = []
        for path in self.sessions.glob("*.json"):
            record = self.read_json(path)
            if record and record.get("schemaVersion") == SCHEMA_SESSION:
                records.append(record)
        return records

    def prune_sessions(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_LOOKBACK_DAYS)
        removed = 0
        for path in self.sessions.glob("*.json"):
            record = self.read_json(path)
            try:
                stale = record is None or parse_utc(record["updatedAt"]) < cutoff
            except (KeyError, ValueError):
                stale = True
            if stale:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # ---- lease ----------------------------------------------------------------

    @property
    def lease_path(self) -> Path:
        return self.leases / "lease.json"

    def load_lease(self) -> dict | None:
        lease = self.read_json(self.lease_path)
        if not lease or lease.get("schemaVersion") != SCHEMA_LEASE:
            return None
        try:
            if parse_utc(lease["expiresAt"]) <= datetime.now(timezone.utc):
                return None  # expired: atomically replaceable
        except (KeyError, ValueError):
            return None
        return lease

    def try_acquire_provisional(self, owner: str, runtime_id: str | None, reason: str) -> dict | None:
        if self.load_lease() is not None:
            return None
        now = utcnow()
        lease = {
            "schemaVersion": SCHEMA_LEASE,
            "ownerSessionId": owner,
            "checkpointId": None,
            "leaseKind": "provisional",
            "triggerReason": reason,
            "runtimeInstanceId": runtime_id,
            "acquiredAt": now,
            "renewedAt": now,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=PROVISIONAL_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pid": os.getpid(),
        }
        self.write_json(self.lease_path, lease)
        return lease

    def lease_renew(self, owner: str, lease_kind: str, ttl_seconds: int, checkpoint_id: str | None) -> tuple[bool, str]:
        lease = self.read_json(self.lease_path)
        if lease is None:
            return False, "no-lease-file"
        try:
            expired = parse_utc(lease["expiresAt"]) <= datetime.now(timezone.utc)
        except (KeyError, ValueError):
            expired = True
        if lease.get("ownerSessionId") != owner and not expired:
            return False, "owned-by-other-session"
        if lease.get("leaseKind") == "coordinator" and lease_kind == "provisional":
            return False, "coordinator-to-provisional-rejected"
        now = datetime.now(timezone.utc)
        lease.update(
            ownerSessionId=owner,
            leaseKind=lease_kind,
            checkpointId=checkpoint_id if checkpoint_id is not None else lease.get("checkpointId"),
            renewedAt=utcnow(),
            expiresAt=(now + timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.write_json(self.lease_path, lease)
        return True, "renewed"

    def lease_release(self, owner: str) -> tuple[bool, str]:
        lease = self.read_json(self.lease_path)
        if lease is None:
            return True, "absent"
        if lease.get("ownerSessionId") != owner:
            return False, "owned-by-other-session"
        self.lease_path.unlink(missing_ok=True)
        return True, "released"

    # ---- manifests --------------------------------------------------------------

    def commit_manifest(self, manifest: dict) -> tuple[bool, str, Path | None]:
        if manifest.get("schemaVersion") != SCHEMA_MANIFEST:
            return False, "wrong-schemaVersion", None
        if manifest.get("mode") not in {"planned", "unexpected"}:
            return False, "invalid-mode", None
        if manifest.get("stage") not in MANIFEST_STAGES:
            return False, "invalid-stage", None
        checkpoint_id = manifest.get("checkpointId")
        if not isinstance(checkpoint_id, str) or not checkpoint_id or any(c in checkpoint_id for c in '\\/:*"<>|'):
            return False, "invalid-checkpointId", None
        manifest["updatedAt"] = utcnow()
        path = self.manifests / (checkpoint_id + ".json")
        self.write_json(path, manifest)
        digest = sha256_text(path.read_text(encoding="utf-8"))
        (self.manifests / (checkpoint_id + ".sha256")).write_text(digest + "\n", encoding="utf-8")
        self.write_json(self.manifests / "latest.json", {"checkpointId": checkpoint_id, "updatedAt": manifest["updatedAt"]})
        return True, "committed", path

    def verify_manifest(self, checkpoint_id: str | None) -> tuple[bool, str, dict | None]:
        if checkpoint_id is None:
            pointer = self.read_json(self.manifests / "latest.json")
            if not pointer or "checkpointId" not in pointer:
                return False, "no-latest-pointer", None
            checkpoint_id = pointer["checkpointId"]
        path = self.manifests / (checkpoint_id + ".json")
        manifest = self.read_json(path)
        if manifest is None:
            return False, "manifest-missing", None
        digest_path = self.manifests / (checkpoint_id + ".sha256")
        try:
            expected = digest_path.read_text(encoding="utf-8").strip()
        except OSError:
            return False, "digest-missing", manifest
        actual = sha256_text(path.read_text(encoding="utf-8"))
        if expected != actual:
            return False, "hash-mismatch", manifest
        return True, "verified", manifest

    def latest_manifest(self, stages: set[str] | None = None) -> dict | None:
        best: dict | None = None
        for path in self.manifests.glob("*.json"):
            if path.name in {"latest.json"}:
                continue
            manifest = self.read_json(path)
            if not manifest or manifest.get("schemaVersion") != SCHEMA_MANIFEST:
                continue
            if stages is not None and manifest.get("stage") not in stages:
                continue
            if best is None or str(manifest.get("updatedAt", "")) > str(best.get("updatedAt", "")):
                best = manifest
        return best

    # ---- diagnostics ---------------------------------------------------------------

    @property
    def diagnostics_control(self) -> Path:
        return self.diagnostics / "control.json"

    def diagnostics_armed(self) -> dict | None:
        control = self.read_json(self.diagnostics_control)
        if not control or not control.get("armed"):
            return None
        try:
            if parse_utc(control["until"]) <= datetime.now(timezone.utc):
                return None
        except (KeyError, ValueError):
            return None
        return control

    def record_diagnostic(self, payload: dict) -> None:
        payload["schemaVersion"] = SCHEMA_DIAGNOSTIC
        invocation = f"{int(time.time() * 1000)}-{os.getpid()}-{sha256_text(json.dumps(payload, sort_keys=True))[:8]}"
        self.write_json(self.diagnostics / "events" / (invocation + ".json"), payload)
        events = sorted((self.diagnostics / "events").glob("*.json"))
        overflow = len(events) - DIAGNOSTICS_RETENTION
        for path in events[:max(0, overflow)]:
            path.unlink(missing_ok=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=DIAGNOSTICS_MAX_AGE_DAYS)
        for path in events:
            if path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)


def probe_runtime_identity() -> tuple[str | None, str]:
    """Hash the bounded parent-PID chain of this hook process.

    Persists only the SHA-256 and a status; never command lines or paths.
    Same chain => same ZCode host process (normal concurrency); a different
    non-empty chain is interruption evidence. Unavailable => manual fallback.
    """
    try:
        class ProcessEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snapshot == -1 or snapshot == 0xFFFFFFFF:
            return None, "unavailable"
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        by_pid: dict[int, int] = {}
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            by_pid[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
        chain: list[int] = []
        pid = os.getpid()
        for _ in range(8):
            chain.append(pid)
            parent = by_pid.get(pid)
            if parent is None or parent == 0 or parent == pid or parent in chain:
                break
            pid = parent
        return sha256_text(".".join(str(p) for p in chain)), "ok"
    except Exception:
        return None, "unavailable"


def extract_field(payload: dict, *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def classify_pending(sessions: list[dict], current_session: str, current_runtime: str | None) -> dict:
    """Split unresolved sessions into interrupted / concurrent / manual-fallback."""
    now = datetime.now(timezone.utc)
    horizon = now - timedelta(days=SESSION_LOOKBACK_DAYS)
    interrupted, concurrent, fallback = [], [], []
    for record in sessions:
        if record.get("sessionId") == current_session:
            continue
        if not record.get("openTurn"):
            continue
        try:
            if parse_utc(record["updatedAt"]) < horizon:
                continue
        except (KeyError, ValueError):
            continue
        candidate_runtime = record.get("runtimeInstanceId")
        entry = {
            "sessionId": record["sessionId"],
            "updatedAt": record.get("updatedAt"),
            "openToolCount": len(record.get("openTools", [])),
            "promptCount": record.get("promptCount", 0),
            "lastToolName": record.get("lastToolName"),
        }
        if candidate_runtime and current_runtime and candidate_runtime != current_runtime:
            interrupted.append(entry)
        elif candidate_runtime and candidate_runtime == current_runtime:
            concurrent.append(entry)
        else:
            fallback.append(entry)
    return {"interrupted": interrupted, "concurrent": concurrent, "manualFallback": fallback}


def build_recovery_context(pending: dict, lease: dict) -> str:
    lines = [
        "ZCode task recovery pending. Interrupted sessions with an open turn were found",
        f"(lease: {lease.get('leaseKind')}, acquired {lease.get('acquiredAt')}, expires {lease.get('expiresAt')}).",
        "This is lifecycle metadata only; no prompts or commands are stored.",
    ]
    for name, entries in (("interrupted", pending["interrupted"]), ("manual-fallback", pending["manualFallback"])):
        for entry in entries[:5]:
            lines.append(
                f"- {name}: session {entry['sessionId']} last active {entry['updatedAt']}, "
                f"{entry['promptCount']} prompts, open tools: {entry['openToolCount']}, last tool: {entry['lastToolName']}"
            )
    lines.append("Use the zcode-task-recovery skill (status / recover modes) before writing anything. Do not assume failure; reconcile read-only first.")
    text = "\n".join(lines)
    return text[:RECOVERY_CONTEXT_LIMIT_CHARS]


def handle_hook(state: State, payload: dict) -> tuple[dict | None, int]:
    """Apply one lifecycle event. Returns (optional hook output JSON, exit code)."""
    event = extract_field(payload, "hook_event_name", "event_name", "eventName") or "other"
    session_id = extract_field(payload, "session_id", "sessionId") or "unknown-session"
    source = extract_field(payload, "source", "sessionSource", "matcherValue")
    tool_name = extract_field(payload, "tool_name", "toolName")

    if event not in MANAGED_EVENTS:
        return None, 0

    diagnostic = {"event": event, "stage": "entered", "at": utcnow()}
    armed = state.diagnostics_armed() is not None

    record = state.load_session(session_id)
    if event == "SessionStart":
        record["lastSource"] = source
        record["openTurn"] = False
        record["openTools"] = []
    elif event == "UserPromptSubmit":
        record["promptCount"] += 1
        record["lastPromptAt"] = utcnow()
        record["openTurn"] = True
    elif event == "PreToolUse":
        name = str(tool_name or "unknown")
        record["toolCount"] += 1
        record["lastToolName"] = name
        if len(record["openTools"]) < MAX_OPEN_TOOLS:
            record["openTools"].append({"toolName": name, "openedAt": utcnow()})
        record["openTurn"] = True
    elif event in {"PostToolUse", "PostToolUseFailure"}:
        name = str(tool_name or "unknown")
        record["lastToolName"] = name
        for index, open_tool in enumerate(record["openTools"]):
            if open_tool["toolName"] == name:
                record["openTools"].pop(index)
                break
        record["toolResultUnknownCount"] = len(record["openTools"])
    elif event == "PermissionRequest":
        record["permCount"] += 1
    elif event == "Stop":
        record["lastStopAt"] = utcnow()
        record["toolResultUnknownCount"] += len(record["openTools"])
        record["openTools"] = []
        record["openTurn"] = False

    runtime_id, runtime_status = probe_runtime_identity()
    record["runtimeInstanceId"] = runtime_id
    record["runtimeProbeStatus"] = runtime_status
    record["lastEvent"] = event
    state.save_session(record)
    state.prune_sessions()
    diagnostic["stage"] = "state-written"

    output: dict | None = None
    if event == "SessionStart" and source in {"startup", "resume"}:
        pending = classify_pending(state.iter_sessions(), session_id, runtime_id)
        if pending["interrupted"]:
            reason = "unexpected-runtime-change"
            lease = state.try_acquire_provisional(session_id, runtime_id, reason)
            if lease is not None:
                output = {"additionalContext": build_recovery_context(pending, lease)}
                diagnostic["leaseObservation"] = "provisional-acquired"
            else:
                diagnostic["leaseObservation"] = "lease-held"
        else:
            diagnostic["leaseObservation"] = "no-interrupted-sessions"

    if armed:
        diagnostic["stage"] = "returned"
        state.record_diagnostic(diagnostic)
    return output, 0


def resolve_state_root(zcode_home: str | None) -> Path:
    home = Path(zcode_home) if zcode_home else Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".zcode"
    return home / STATE_RELATIVE


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    try:
        state = State(resolve_state_root(args.zcode_home))
        output, code = handle_hook(state, payload if isinstance(payload, dict) else {})
        if output:
            print(json.dumps(output, ensure_ascii=False))
        return code
    except Exception:
        # Fail open: never block a session on internal state errors.
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = State(resolve_state_root(args.zcode_home))
    lease = state.load_lease()
    sessions = state.iter_sessions()
    pending = classify_pending(sessions, "", None)
    ok, verify_message, latest = state.verify_manifest(None)
    report = {
        "schemaVersion": "zcode-continuity-status.v1",
        "generatedAt": utcnow(),
        "lease": lease,
        "pendingSessions": {
            "interrupted": pending["interrupted"],
            "concurrentOpen": pending["concurrent"],
            "manualFallback": pending["manualFallback"],
        },
        "pendingManifest": state.latest_manifest(AUTOMATIC_STAGES),
        "attentionManifest": state.latest_manifest({"needs-attention"}),
        "latestManifest": state.latest_manifest(None),
        "latestManifestVerify": {"ok": ok, "result": verify_message},
        "sessionCount": len(sessions),
        "hookInstallation": "plugin hooks/hooks.json (all seven ZCode events)",
        "stateRoot": str(state.root),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_lease(args: argparse.Namespace) -> int:
    state = State(resolve_state_root(args.zcode_home))
    if args.lease_action == "renew":
        ok, message = state.lease_renew(args.owner, args.lease_kind, args.ttl_seconds, args.checkpoint_id)
        print(json.dumps({"ok": ok, "result": message}, ensure_ascii=False))
        return 0 if ok else 1
    if args.lease_action == "release":
        ok, message = state.lease_release(args.owner)
        print(json.dumps({"ok": ok, "result": message}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


def cmd_manifest(args: argparse.Namespace) -> int:
    state = State(resolve_state_root(args.zcode_home))
    if args.manifest_action == "commit":
        try:
            manifest = json.loads(sys.stdin.read())
        except ValueError:
            print(json.dumps({"ok": False, "result": "stdin-not-valid-json"}, ensure_ascii=False))
            return 1
        ok, message, path = state.commit_manifest(manifest)
        print(json.dumps({"ok": ok, "result": message, "path": str(path) if path else None}, ensure_ascii=False))
        return 0 if ok else 1
    if args.manifest_action == "verify":
        ok, message, manifest = state.verify_manifest(args.checkpoint_id)
        print(json.dumps({"ok": ok, "result": message, "checkpointId": manifest.get("checkpointId") if manifest else None, "stage": manifest.get("stage") if manifest else None}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


def cmd_diagnostics(args: argparse.Namespace) -> int:
    state = State(resolve_state_root(args.zcode_home))
    if args.diagnostics_action == "arm":
        until = (datetime.now(timezone.utc) + timedelta(minutes=args.ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state.write_json(state.diagnostics_control, {"armed": True, "until": until})
        print(json.dumps({"armed": True, "until": until}, ensure_ascii=False))
    elif args.diagnostics_action == "disarm":
        state.write_json(state.diagnostics_control, {"armed": False, "until": None})
        print(json.dumps({"armed": False}, ensure_ascii=False))
    elif args.diagnostics_action == "status":
        control = state.read_json(state.diagnostics_control) or {}
        events = sorted((state.diagnostics / "events").glob("*.json"))
        recent = [state.read_json(path) for path in events[-10:]]
        armed = state.diagnostics_armed() is not None
        print(json.dumps({"armed": armed, "control": control, "eventCount": len(events), "recent": recent}, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Self-test against a throwaway state root (never the live root)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        state = State(Path(tmp) / STATE_RELATIVE)
        payload_a = {"hook_event_name": "UserPromptSubmit", "session_id": "sess-a"}
        out, code = handle_hook(state, payload_a)
        assert code == 0 and out is None
        out, _ = handle_hook(state, {"hook_event_name": "PreToolUse", "session_id": "sess-a", "tool_name": "Bash"})
        record = state.load_session("sess-a")
        assert record["openTurn"] and record["openTools"] and record["promptCount"] == 1
        handle_hook(state, {"hook_event_name": "PostToolUse", "session_id": "sess-a", "tool_name": "Bash"})
        record = state.load_session("sess-a")
        assert not record["openTools"] and record["openTurn"]
        handle_hook(state, {"hook_event_name": "Stop", "session_id": "sess-a"})
        record = state.load_session("sess-a")
        assert not record["openTurn"]
        # Simulate an interrupted session with a foreign runtime, then a fresh startup.
        stale = state.load_session("sess-b")
        stale.update(openTurn=True, runtimeInstanceId="deadbeef" * 8, promptCount=3, updatedAt=utcnow())
        state.save_session(stale)
        out, code = handle_hook(state, {"hook_event_name": "SessionStart", "session_id": "sess-c", "source": "startup"})
        assert code == 0 and out is not None and "additionalContext" in out
        lease = state.load_lease()
        assert lease and lease["leaseKind"] == "provisional" and lease["ownerSessionId"] == "sess-c"
        ok, message = state.lease_renew("sess-c", "coordinator", 900, "doctor-checkpoint")
        assert ok, message
        manifest = {
            "schemaVersion": SCHEMA_MANIFEST,
            "checkpointId": "doctor-checkpoint",
            "mode": "unexpected",
            "stage": "recovering",
            "createdAt": utcnow(),
            "tasks": [],
        }
        ok, message, _ = state.commit_manifest(manifest)
        assert ok, message
        ok, message, verified = state.verify_manifest("doctor-checkpoint")
        assert ok and verified["stage"] == "recovering"
        ok, message = state.lease_release("sess-c")
        assert ok, message
        assert state.load_lease() is None
    print("doctor: passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZCode task continuity (recovery hooks, lease, manifests)")
    parser.add_argument("--zcode-home", default=None, help="Explicit ZCode home directory (defaults to %%USERPROFILE%%\\.zcode)")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="Hook mode: read one lifecycle event from stdin")
    hook.set_defaults(func=cmd_hook)

    status = sub.add_parser("status", help="Report leases, pending sessions, and manifests")
    status.add_argument("--json", action="store_true", help="Output JSON (default; accepted for interface compatibility)")
    status.set_defaults(func=cmd_status)

    lease = sub.add_parser("lease", help="Manage the recovery lease")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)
    renew = lease_sub.add_parser("renew")
    renew.add_argument("--owner", required=True)
    renew.add_argument("--checkpoint-id", default=None)
    renew.add_argument("--lease-kind", choices=["provisional", "coordinator"], default="coordinator")
    renew.add_argument("--ttl-seconds", type=int, default=DEFAULT_COORDINATOR_TTL_SECONDS)
    release = lease_sub.add_parser("release")
    release.add_argument("--owner", required=True)
    lease.set_defaults(func=cmd_lease)

    manifest = sub.add_parser("manifest", help="Commit or verify recovery manifests")
    manifest_sub = manifest.add_subparsers(dest="manifest_action", required=True)
    commit = manifest_sub.add_parser("commit")
    commit.set_defaults(manifest_action="commit")
    verify = manifest_sub.add_parser("verify")
    verify.add_argument("--latest", action="store_true")
    verify.add_argument("--checkpoint-id", default=None)
    verify.set_defaults(manifest_action="verify")
    manifest.set_defaults(func=cmd_manifest)

    diagnostics = sub.add_parser("diagnostics", help="Live-dispatch diagnostics window")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_action", required=True)
    arm = diagnostics_sub.add_parser("arm")
    arm.add_argument("--ttl-minutes", type=int, default=30)
    diagnostics_sub.add_parser("disarm")
    diagnostics_sub.add_parser("status")
    diagnostics.set_defaults(func=cmd_diagnostics)

    doctor = sub.add_parser("doctor", help="Self-test against a temporary state root")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
