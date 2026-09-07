# Skills

Reusable instructions and workflows that give coding agents focused
capabilities.

## Layout

```text
skills/
├── claude-code/   Skills that depend on Claude Code
├── codex/         Skills that depend on Codex
└── shared/        Portable skills that work with both
```

Current collection:

| Runtime | Skills |
| --- | --- |
| Claude Code | `codex-session-messaging` |
| Codex | `claude-session-messaging`, `codex-agent-messaging` |
| Shared | `karpathy-guidelines`, `show-me` |

## Install

Copy the skill you want into the target project's discovery directory:

```text
.claude/skills/<skill-name>/   # Claude Code
.codex/skills/<skill-name>/    # Codex
```

For a shared skill, use the directory for the runtime you are working with.
Keep each skill self-contained, with `SKILL.md` at its root.
