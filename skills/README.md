# Skills

Reusable instructions and workflows that give coding agents focused
capabilities.

## Layout

```text
skills/
├── claude-code/   Skills that depend on Claude Code
├── codex/         Skills that depend on Codex
├── shared/        Portable skills that work with both
└── mattpocock/    Vendored Matt Pocock skill collection
```

Current collection:

| Runtime | Skills |
| --- | --- |
| Claude Code | `codex-session-messaging` |
| Codex | `claude-session-messaging`, `codex-agent-messaging` |
| Shared | `archify`, `karpathy-guidelines`, `show-me` |

External collections stay in their own namespace and are not redistributed
across the runtime folders:

| Collection | Contents |
| --- | --- |
| [`mattpocock/`](mattpocock/) | 38 skills grouped by their upstream buckets |

## Install

Copy the skill you want into the target project's discovery directory:

```text
.claude/skills/<skill-name>/   # Claude Code
.codex/skills/<skill-name>/    # Codex
```

For a shared skill, use the directory for the runtime you are working with.
For an external collection, copy the individual nested skill directory rather
than the whole collection. Keep each skill self-contained, with `SKILL.md` at
its root.
