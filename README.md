# agent-kitbag

My working setup for coding agents—configs, skills, and plugins. Steal what's useful.

This repository is a small, practical collection of the pieces I reuse across
coding-agent workflows. Everything is organized to be easy to inspect, copy,
and adapt.

## What's inside

```text
agent-kitbag/
├── configs/           Agent and tool configuration
├── skills/
│   ├── claude-code/   Claude Code-only skills
│   ├── codex/         Codex-only skills
│   └── shared/        Skills that work with both
└── plugins/           Plugin packages and integrations
```

Each directory contains its own README with the expected layout and naming
conventions.

## Use it

Clone the repository:

```bash
git clone https://github.com/zzh-SJTU/agent-kitbag.git
cd agent-kitbag
```

Then copy only what you need into your own setup. Review files before using
them: paths, commands, permissions, and available tools can differ between
environments.

## Principles

- Keep every item focused and understandable on its own.
- Prefer portable defaults over machine-specific assumptions.
- Document prerequisites and non-obvious behavior close to the files.
- Never commit credentials, tokens, or other private data.

## Contributing

Suggestions and improvements are welcome. Keep additions small, explain what
they are for, and include a short usage example when helpful.

## License

[MIT](LICENSE)
