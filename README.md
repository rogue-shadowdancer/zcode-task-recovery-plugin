# zcode-task-recovery-plugin

Preserve, audit, and resume unfinished **ZCode** tasks across restarts, shutdowns, updates, crashes, blue screens, and power loss.

This is the GLM/ZCode adaptation of the [recover-codex-tasks](https://github.com/rogue-shadowdancer) continuity design for Codex: the lifecycle-hook checkpointing, the single-writer recovery lease, the manifest contract, the eight task classes, and the fail-closed privacy invariants carry over; Codex thread-dispatch tools are replaced by session-level reconciliation because ZCode exposes no thread-listing capability.

## What it does

- **Lifecycle hooks** on all seven ZCode events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `Stop`) record per-session turn state under `~/.zcode/task-continuity/v1/` — counts, tool names, timestamps, and a hashed runtime identity only. No prompts, commands, tool inputs/outputs, replies, credentials, or environment values are ever stored.
- **Interruption detection**: an unresolved open turn from a *different* ZCode host process (hashed parent-PID chain) is interruption evidence; the same runtime is normal concurrency; a missing identity is manual fallback.
- **Recovery lease**: a qualifying `SessionStart(startup|resume)` may hold one 90-second provisional claim and inject bounded `additionalContext` telling the agent a recovery is pending; the agent promotes it to a 15-minute coordinator lease while it reconciles. One writer per repository or gate, always.
- **Manifests** (`zcode-recovery-manifest.v1`): planned-restart and unexpected-interruption inventories with SHA-256 integrity; only `pending-restart`/`recovering` stages can trigger recovery.
- **Read-only reconciliation first**: a `PreToolUse` without its result, or a `Stop` that never came, is *unknown* — never assumed failed. Git HEAD, PR state, checks, tags, and operation IDs are read back before anything is retried.

## Install

ZCode → Plugin Marketplace → Add → Add Plugin Marketplace, paste this repository (or a local clone of it), then install **zcode-task-recovery** from the **Personal** section. The hooks ship with the plugin; no hand-editing of hook config is needed. Verify with:

```powershell
C:\Windows\py.exe -3 "<plugin-root>\hooks\continuity.py" status --json
```

## Use

Tell ZCode things like "我要重启了，保存现场" (prepare), "电脑蓝屏了，恢复所有任务" (recover), or "恢复状态怎么样" (status). The skill picks the mode; see `skills/zcode-task-recovery/SKILL.md` and the contract in `skills/zcode-task-recovery/references/recovery-contract.md`.

Acceptance window for hook dispatch evidence:

```powershell
C:\Windows\py.exe -3 "<plugin-root>\hooks\continuity.py" diagnostics arm --ttl-minutes 30
#   ... exercise a fresh ZCode session (prompt, tools, stop), then:
C:\Windows\py.exe -3 "<plugin-root>\hooks\continuity.py" diagnostics status --json
C:\Windows\py.exe -3 "<plugin-root>\hooks\continuity.py" diagnostics disarm
```

## Tests

```powershell
C:\Windows\py.exe -3 tests\test_continuity.py
C:\Windows\py.exe -3 hooks\continuity.py doctor
```

## License

MIT © rogue-shadowdancer
