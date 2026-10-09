# Recovery Contract (ZCode adaptation, v1)

## Contents

1. State layout
2. Manifest contract
3. Lifecycle evidence
4. Recovery coordinator and lease
5. Task classes
6. Authorization rules
7. Planned-restart message
8. Recovery message
9. Completion rules

## State layout

All state lives under `<zcode-home>/task-continuity/v1/` as atomic UTF-8 JSON (`~/.zcode/task-continuity/v1/` by default; override with `--zcode-home`):

```text
sessions/<sha256(sessionId)[:24]>.json   lifecycle records (retention: 14 days)
leases/lease.json                        the single active recovery lease
manifests/<checkpointId>.json|.sha256    recovery manifests + integrity hash
manifests/latest.json                    best-effort pointer (rebuildable)
diagnostics/control.json                 armed acceptance window
diagnostics/events/<invocation>.json     redacted dispatch evidence (200 records / 7 days)
```

Sessions are keyed by hash; the raw session id is stored inside the record for reporting only. No prompts, commands, tool inputs/outputs, replies, credentials, or environment values are ever persisted.

## Manifest contract

Commit UTF-8 JSON with `schemaVersion: "zcode-recovery-manifest.v1"`, a unique path-safe `checkpointId`, one `mode` (`planned` | `unexpected`), and one `stage` (`preparing` | `pending-restart` | `recovering` | `needs-attention` | `complete`):

```json
{
  "schemaVersion": "zcode-recovery-manifest.v1",
  "checkpointId": "2026-10-09T09-00-00Z-example",
  "mode": "planned",
  "stage": "preparing",
  "createdAt": "2026-10-09T09:00:00Z",
  "coordinator": {},
  "tasks": [],
  "automations": [],
  "blockers": []
}
```

For every task include at minimum: `taskKey` (session id or user-visible task name), `cwd`, `objective`, `observedStatus`, `lastCompletedStep`, `interruptedStep`, `nextSafeStep`, `workspace`, `classification`, `recoveryAction`, `blockers`. JSON is authoritative; `continuity.py manifest commit` adds the integrity hash and updates the pointer. Never hand-edit a committed manifest.

Only `pending-restart` and `recovering` are automatic-recovery stages; `preparing`, `needs-attention`, and `complete` are audit state and never trigger recovery.

## Lifecycle evidence

- `UserPromptSubmit` starts an in-flight root turn (`openTurn: true`).
- `PreToolUse` without its matching `PostToolUse`/`PostToolUseFailure` is an unknown tool result requiring read-only reconciliation.
- `Stop` closes the turn; open tools at `Stop` are counted as `toolResultUnknown`.
- The next eligible root `SessionStart` with source `startup` or `resume` discovers unresolved state and may coordinate recovery. `clear` and `compact` are recorded but never acquire a lease.
- Runtime identity is the SHA-256 of this hook process's bounded parent-PID chain. Only the hash and `runtimeProbeStatus: ok|unavailable` are persisted. Unresolved sessions with the same non-empty runtime identity are normal concurrency; a different non-empty identity is interruption evidence; missing identity is manual fallback.
- `SessionEnd` does not exist in ZCode and is never depended on.

## Recovery coordinator and lease

`zcode-recovery-lease.v1`:

```json
{
  "schemaVersion": "zcode-recovery-lease.v1",
  "ownerSessionId": "<session-id>",
  "checkpointId": "<checkpoint-id|null>",
  "leaseKind": "provisional|coordinator",
  "triggerReason": "unexpected-runtime-change",
  "runtimeInstanceId": "<sha256|null>",
  "acquiredAt": "<UTC>",
  "renewedAt": "<UTC>",
  "expiresAt": "<UTC>",
  "pid": 12345
}
```

A qualifying `SessionStart(startup|resume)` with real interruption evidence may hold one 90-second `provisional` claim and inject bounded recovery context (`additionalContext`). Promotion: `lease renew --owner <id> --checkpoint-id <id> --lease-kind coordinator --ttl-seconds 900`. `provisional -> coordinator` only; `coordinator -> provisional` is rejected. Release explicitly after the final manifest is committed and verified. An expired lease is atomically replaceable.

## Task classes

| Class | Meaning | Action |
|---|---|---|
| `completed` | Completion is proven | Do not wake |
| `waiting-user` | The original task needs user input | Preserve and report |
| `blocked` | A real blocker remains | Preserve and report |
| `already-running` | The work is executing (same runtime) | Record as active; do not duplicate or monitor |
| `interrupted-safe` | Read-only, local, or proven idempotent work was interrupted | Reconcile, then continue in the original session |
| `side-effect-unknown` | A write or external tool began without a confirmed result | Reconcile read-only before continuing |
| `unreachable` | Session or source cannot be inspected | Keep pending; do not clone |
| `unacknowledged` | Planned-pause request was not acknowledged | Preserve last evidence and warn |

## Authorization rules

- Preserve ordinary and standing authorization through a restart when scope and target are unchanged.
- Do not broaden repository, host, service, deployment, release, VM, device, or data scope.
- Treat an explicit one-shot action as consumed if readback proves it happened.
- Request fresh confirmation only for revoked/expired authority, a changed exact high-risk delta, an unresolvable ambiguous partial effect, or a higher-priority rule requiring separate confirmation.

## `PrepareForRestart v1`

```text
PrepareForRestart v1
checkpointId: <id>
deadline: <UTC or none>
Stop before starting another external write. Finish only an already-entered atomic step when safe.
Return: objective; gate; authorization basis and one-shot state; branch/HEAD/upstream;
staged/unstaged/untracked and user-owned changes; external refs and operation IDs;
last completed step; in-flight or ambiguous step; validation; blockers; task-owned processes;
next safe step; recommended recovery class.
Do not stash, reset, clean, checkout, roll back, delete, publish, deploy, or broaden scope.
```

## `TaskRecoveryEnvelope v1`

```text
TaskRecoveryEnvelope v1
checkpointId: <id>
interruptionMode: planned|unexpected
verifiedState: <readback summary>
doNotRepeat: <completed or ambiguous effects>
authorization: inherited from the original session; not expanded
nextSafeStep: <one concrete step>
Continue in the original session. Re-read live state before any external write.
Report actual progress, new blockers, and any authority that genuinely requires renewal there.
Continue independently; the recovery coordinator will not monitor this task after handover.
```

## Completion rules

- Handover (the user reopening the affected session with the envelope delivered) is the coordinator's launch evidence; it proves nothing about later progress.
- After handover, do not poll or monitor the continued session's progress; the continuing session reports on its own.
- Restore at most one writer for a repository or gate.
- `complete` only when every task is proven terminal, intentionally not woken, already running, or handed over. Otherwise `needs-attention`.
- Release the lease only after the final manifest is committed and verified.
