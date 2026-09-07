---
name: claude-session-messaging
description: Send scoped messages from Codex to already-running local Claude Code sessions and verify structured replies. Use for cross-runtime collaboration with independent Claude sessions; do not use to start new Claude sessions or bypass usage authorization.
---

# Claude Session Messaging

Use this skill only for an already-running local Claude Code session. It uses Claude
Code's local authenticated peer inbox and reads only log bytes appended after the
request.

`SKILL_DIR` below means this skill directory.

## Workflow

List exact peers in the intended workspace:

```text
python -B "SKILL_DIR/scripts/claude_comm.py" peers --workspace /project
```

Verify the peer inbox without submitting a model prompt:

```text
python -B "SKILL_DIR/scripts/claude_comm.py" probe \
  --to SESSION_ID --workspace /project
```

Sending a request can invoke the target Claude provider and spend its configured
quota. Obtain explicit current authority before this step:

```text
python -B "SKILL_DIR/scripts/claude_comm.py" request \
  --to SESSION_ID --workspace /project \
  --request-id TASK_ID --wait-seconds 120 --include-text \
  --text "Scoped objective, permissions, checks, and desired reply."
```

Claude may hold a message from an unidentified cross-runtime peer for user approval.
Respect that gate. Do not change `crossSessionInbound` or claim a Claude permission
mode on Codex's behalf.

If the request was sent without waiting, reuse the returned `cursor`:

```text
python -B "SKILL_DIR/scripts/claude_comm.py" wait \
  --to SESSION_ID --workspace /project \
  --request-id TASK_ID --cursor CURSOR --wait-seconds 120 --include-text
```

## Rules

- Resolve one exact active session by session ID or unique name and workspace.
- Keep one request concise; include all permission and scope boundaries in it.
- Use a new unique `request_id` for new work. Reuse a prior request only with `wait`
  and its original cursor; never resend it.
- Never print or persist the Claude peer token.
- Never scan or summarize existing Claude conversation text. Read only bytes appended
  after the request cursor and accept only a matching structured reply.
- Do not automatically retry an uncertain request or start/resume another Claude
  session as fallback. A `send_uncertain` result includes the pre-send cursor; use
  `wait` with that cursor because the message may already be delivered.
- A held message is pending, not failed. Wait for the user to approve or decline it;
  do not resend it.
- Messaging does not grant file, Git, API, credential, deployment, or paid-usage
  authority.
- Keep the existing `codex-agent-messaging` skill for Codex-to-Codex communication;
  the protocols are separate.

Read [references/protocol.md](references/protocol.md) only when maintaining the bridge
or troubleshooting local peer compatibility.
