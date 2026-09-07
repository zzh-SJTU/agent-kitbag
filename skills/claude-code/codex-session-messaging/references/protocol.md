# claude-codex-message/1 protocol

Cross-runtime bridge for **Claude -> Codex**. Independent of Codex's
`codex-agent-message/2` (Codex↔Codex) and `codex-claude-message/1` (Codex→Claude).
The three protocols share no frames and must not be mixed.

## Transport

- **Send**: `codex queue --thread <UUID> --message <TEXT>`. The message is a
  single first-line `CLAUDE_CODEX_MESSAGE ` JSON frame, followed by the human-
  readable body and the reply contract. `codex queue` prints
  `Queued message <id> for thread <UUID>.`; the helper confirms the thread id
  matches and treats any other outcome as `send_uncertain`.
- **Readback**: query-only against `CODEX_HOME` (default `~/.codex`)
  `thread_history_*.sqlite`, table `thread_items`. Codex stores an assistant
  reply as an `item_type='agentMessage'` row whose `item_json` is
  `{"type":"agentMessage","text":"...","phase":"final_answer", ...}`. The reply
  frame is parsed from that `text`.

Connections use `mode=ro` + `PRAGMA query_only=ON`. The helper validates required
columns and stops on an incompatible schema. It never writes, migrates, or repairs
Codex stores, and never removes WAL/SHM files.

## Cursor

Before sending, the helper records `cursor = MAX(rollout_ordinal)` over the
target thread's `thread_items`. Readback considers only `agentMessage` rows with
`rollout_ordinal > cursor` **and** `instr(item_json, request_id) > 0`. This scopes
reads to new, on-topic items and avoids scanning unrelated history.

On an uncertain send (`send_uncertain`), the helper returns that same pre-send
`cursor` (plus `request_id` and `to_session_id`) so the caller can re-check with
`wait` for a possibly-delivered reply rather than resending, which could
double-deliver. `codex queue` has no request-id transport dedupe, so the sender
must not resend.

## Peer ordering

`peers` returns active (`archived=0`) sessions ordered most-recently-active
first, using the first available of `recency_at_ms`, `updated_at_ms`, or
`created_at_ms`. That column is probed at read time and is **not** part of the
required-schema check, so discovery still works on stores that lack it (recency
is reported as `null` and rows fall back to name/id order).

## Frames

Request. The first line is an **envelope-only** frame (routing/identity, no
body); the human-readable request body follows once as plain text, then the
reply contract. The body is never duplicated inside the frame — the reply frame
never echoes it and readback never reads it.

```json
CLAUDE_CODEX_MESSAGE {"protocol":"claude-codex-message/1","kind":"request","request_id":"<token>","from":"claude","to_session_id":"<uuid>","sent_at_utc":"<ISO-8601 Z>"}
```

Reply (emitted by the Codex agent as ordinary answer text; read from
`agentMessage.text`):

```json
CLAUDE_CODEX_REPLY {"protocol":"claude-codex-message/1","request_id":"<same token>","status":"completed|failed|blocked","body":"<concise response>"}
```

A reply is accepted only when protocol, `request_id`, a terminal `status`, and a
string `body` all validate. Conflicting terminal replies for one request id fail
closed. Because `from` is the literal `"claude"` (Claude has no Codex thread UUID),
Codex's own `agent_comm.py` will not treat these frames as native traffic — that
is deliberate isolation, not a bug.

## Verified environment

Windows 10, Codex CLI 0.153.x, `CODEX_HOME=~/.codex`. `state_*.sqlite.threads`
(`id,name,cwd,archived`, plus optional recency columns) and
`thread_history_*.sqlite.thread_items`
(`thread_id,item_type,item_json,rollout_ordinal`) confirmed on the live store.
This documents the layout the helper depends on rather than pinning an exact
build; the required columns are validated at read time and fail closed if a
future Codex version changes them, so no exact version claim needs upkeep here.
