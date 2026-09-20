# Matt Pocock Skills

A vendored snapshot of [mattpocock/skills](https://github.com/mattpocock/skills),
kept in its own namespace so it does not mix with the repository's runtime
folders.

## Contents

| Bucket | Skills | Status |
| --- | ---: | --- |
| `engineering/` | 18 | Engineering workflows |
| `productivity/` | 7 | Communication and learning workflows |
| `misc/` | 4 | Specialized utilities |
| `in-progress/` | 9 | Experimental; review before use |
| `deprecated/` | 0 | Upstream reference notes |

## Compatibility

All 38 skills include upstream `agents/openai.yaml` metadata. Sixteen pass the
current Codex skill validator unchanged. The other 22 intentionally retain
upstream `disable-model-invocation` and/or `argument-hint` frontmatter fields,
which that validator does not currently accept. This snapshot preserves those
files rather than silently rewriting upstream behavior.

To install one, copy its complete directory into the target agent's skill
discovery directory. For example:

```text
mattpocock/engineering/code-review/ → .codex/skills/code-review/
```

The imported skill files are kept byte-for-byte from the pinned upstream
commit. See [`upstream.json`](upstream.json) for snapshot metadata and
[`LICENSE`](LICENSE) for the upstream MIT license.
