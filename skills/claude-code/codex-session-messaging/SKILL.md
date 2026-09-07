---
name: codex-session-messaging
description: Send scoped messages from this Claude Code session to an already-running local Codex session and verify a structured reply. Use for cross-runtime collaboration with independent Codex sessions; do not use to start new Codex sessions or bypass usage authorization.
---

# Codex Session Messaging

Claude -> Codex counterpart of the Codex-side `claude-session-messaging` skill.
Use it only for an **already-running** local Codex session. Delivery uses the
supported `codex queue` entry point; readback is query-only against Codex's
native `thread_history_*.sqlite` and reads only `agentMessage` items that appear
after the send cursor and contain the request id.

`SKILL_DIR` below means this skill directory.

## Workflow

List exact peers in the intended workspace (prefer a named session, then use its
exact UUID). Results are ordered most-recently-active first (by
`threads.recency_at_ms` when the store exposes it), so among many open sessions
the top row is usually the live peer:

```text
python -B "SKILL_DIR/scripts/codex_comm.py" peers --workspace /project --named-only
```

Sending a request queues a user turn into the target Codex session and can spend
its configured quota. Obtain explicit current authority before this step:

```text
python -B "SKILL_DIR/scripts/codex_comm.py" send \
  --to SESSION_UUID --workspace /project \
  --request-id TASK_ID --wait-seconds 120 --include-text \
  --text "Scoped objective, permissions, checks, and desired reply."
```

If the request was sent without waiting, reuse the returned `cursor`:

```text
python -B "SKILL_DIR/scripts/codex_comm.py" wait \
  --to SESSION_UUID --workspace /project \
  --request-id TASK_ID --cursor CURSOR --wait-seconds 120 --include-text
```

Exit codes: `0` a terminal reply was read, `3` still waiting (a recoverable
pending state — recheck with the same cursor; do not resend), `2` an error or
`send_uncertain` (the queue result was uncertain — inspect before any retry).

## Rules

- Resolve exactly one active session by UUID (and workspace). A workspace with
  many unnamed sessions is ambiguous; supply the exact UUID.
- Keep one request concise; include all permission and scope boundaries in it.
- Read only `agentMessage` items after the cursor that contain the request id.
  Never scan or summarize unrelated Codex conversation content. Accept only a
  matching structured reply.
- A pending reply is not a failure. Recheck with the same cursor; never auto-resend.
- Never retry a `send_uncertain` result automatically — the message may already
  be delivered. The result includes the pre-send `cursor`, `request_id`, and
  `to_session_id`; re-check with `wait --cursor <cursor>` before considering any
  intentional replay (which would reuse the same `request_id`).
- Messaging does not grant file, Git, API, credential, deployment, or paid-usage
  authority, and does not expand task scope.
- Never attach or echo `.env`, `.git` content, credentials, or raw meeting data.
- This is protocol `claude-codex-message/1`. Keep it separate from the Codex-side
  `codex-agent-messaging` (`codex-agent-message/2`) and `claude-session-messaging`
  (`codex-claude-message/1`) skills; the protocols must not be mixed.

Read [references/protocol.md](references/protocol.md) only when maintaining the
bridge or troubleshooting native-store compatibility.

## Tests

Offline tests use disposable Codex-store-shaped sqlite fixtures and a mocked
`codex queue`; they send nothing and touch no live store:

```text
python -B -m unittest discover -s "SKILL_DIR/scripts" -p "test_codex_comm.py" -v
```
