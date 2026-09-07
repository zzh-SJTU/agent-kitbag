---
name: codex-agent-messaging
description: Coordinate independent Codex sessions with correlated requests, progress, supersession, cancellation, concise handoffs, and lifecycle-aware reply verification.
---

# Codex Agent Messaging

Use native collaboration tools for agents in the current collaboration tree. They are
the default because their messages stay inside the agent workflow instead of arriving
as raw `CODEX_AGENT_MESSAGE` user turns.

Use this helper only for independently opened Codex sessions that must communicate
through `codex queue`.

The helper sends native messages and derives lifecycle state from read-only native
metadata. It creates no mailbox, watcher, or background service.

`SKILL_DIR` below means this skill directory.

## Choose the transport

- Current collaboration tree: use native `spawn_agent`, `send_message`, and
  `followup_task`. Do not use this helper.
- Independently opened session: use the quiet path below.

The helper cannot retract or hide a raw message already queued by Codex. Its lifecycle
checks prevent stale work from being processed; quiet mode reduces how many raw
messages are created in the first place.

## Independent-session quiet path

### 1. Find the peer

Prefer a named session in the intended workspace, then use its exact UUID:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" identity
python -B "SKILL_DIR/scripts/agent_comm.py" peers \
  --workspace /project --named-only --exclude-self
```

### 2. Send one scoped request

```text
python -B "SKILL_DIR/scripts/agent_comm.py" send \
  --to PEER_UUID --scope-id WP-123 --revision 1 \
  --text "Objective, allowed paths/actions, checks, and desired reply."
```

Save the returned `request_id` and `message_id`. `request_id` identifies the task;
`message_id` identifies this exact logical message. A queued message proves delivery
acceptance, not that the peer has read it. Quiet mode is the default: the worker sends
no acknowledgement or progress frame and returns one terminal reply only.

The helper rejects a second request when current native state already shows active
work for the same peer and `scope_id`. Use a new request with
`--supersedes OLD_REQUEST_ID` when replacing that work.

### 3. Check and reply

Before external actions and before the terminal reply, check the inbound lifecycle:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" check \
  --peer REQUEST_SENDER_UUID --request-id REQUEST_ID --direction inbound
```

Return one terminal reply:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" reply \
  --request-id REQUEST_ID --status completed \
  --text "PASS. Detailed Handoff: docs/work-packages/WP-123.md" \
  --artifact docs/work-packages/WP-123.md
```

`--to` is optional for progress and replies because the helper infers it from the
request.

For an unusually long task where one state-change update is necessary, the sender
must opt in:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" send \
  --to PEER_UUID --scope-id WP-123 --revision 1 \
  --progress-policy on-change --text "Long scoped task."
```

Then send progress only for a real change such as `waiting` or a newly discovered
blocker. Never send a routine acknowledgement.

### 4. Verify and close

The coordinator checks the lifecycle, records the result in the project's formal
system, then closes the request:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" check \
  --peer PEER_UUID --request-id REQUEST_ID --wait-seconds 30

python -B "SKILL_DIR/scripts/agent_comm.py" close \
  --to PEER_UUID --request-id REQUEST_ID --outcome accepted
```

Closing makes later duplicate or delayed replies stale.

The first terminal reply is retained. Exact repeats are counted as duplicates.
Conflicting later terminal replies produce `terminal_conflict`, block automatic
acceptance, and never replace the first reply.

## Change active work

Replace an old request with a new revision:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" send \
  --to PEER_UUID --scope-id WP-123 --revision 2 \
  --supersedes OLD_REQUEST_ID --text "Replacement scope and checks."
```

Cancel without replacement:

```text
python -B "SKILL_DIR/scripts/agent_comm.py" cancel \
  --to PEER_UUID --request-id REQUEST_ID --text "Stop before further side effects."
```

Native queue delivery cannot interrupt a turn already executing. Do not assume work
stopped until the lifecycle is terminal or authoritative project state proves it.

## Delayed messages

When a raw `CODEX_AGENT_MESSAGE` arrives late, check its peer and request ID before
acting on the body:

- `closed` or `superseded`: stale; do not reprocess it.
- `terminal_conflict`: preserve the first reply and review the conflict; do not accept
  either message silently.
- `completed`, `failed`, or `blocked`: process only the effective terminal reply.
- `acknowledged`, `in_progress`, or `waiting`: work is not finished.

The raw message may still be visible in the UI. `closed`, `superseded`, and
`effective=false` suppress processing, not native UI delivery.

Do not repeat stale message content in a user-facing response. If a response is
unavoidable, state only that a stale Agent message was ignored.

## Rules

- Keep one active request per peer and scope.
- Prefer native collaboration-tree agents whenever reuse of an independent session is
  not required.
- Keep independent-session traffic to one request and one terminal reply by default.
- Keep inline messages concise; attach an authorized shared Handoff for detail.
- Never attach `.env`, `.git` content, credentials, or raw meeting data.
- Messaging does not expand task scope, permissions, API authority, or path ownership.
- Do not retry `send_uncertain` automatically. Inspect the same request first. If it
  remains unobserved and an intentional replay is necessary, reuse the returned
  `request_id` and `message_id` with identical content; the replay is idempotent.

Read [references/lifecycle.md](references/lifecycle.md) for revisions, status meanings,
artifacts, cancellation, and troubleshooting. Read
[references/native-readback.md](references/native-readback.md) only when native-store
compatibility or readback behavior needs investigation.
