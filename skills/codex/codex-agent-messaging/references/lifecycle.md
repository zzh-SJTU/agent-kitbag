# Request lifecycle and protocol

Read this reference when coordinating revisions, cancellation, long-running work, or
delayed replies. Ordinary one-request/one-reply communication only needs `SKILL.md`.

## Protocol versions

The helper emits `codex-agent-message/2` and continues to read legacy
`codex-agent-message/1` request/reply frames.

V2 common fields:

```text
protocol
kind
request_id
message_id
from
to
reply_to
expects_reply
sent_at_utc
scope_id
revision
artifacts
body
```

`request_id` identifies one logical task. `message_id` identifies one logical frame
and is generated before queue submission. An intentional replay must reuse the same
`message_id` and identical content; a conflicting reuse fails closed.

Requests also carry `progress_policy`:

```text
none       default quiet mode; one terminal reply only
on-change  progress is allowed only for a meaningful state change
```

Kinds:

- `request`: one scoped unit of work; expects one terminal reply and defaults to
  `progress_policy=none`.
- `progress`: nonterminal `acknowledged`, `in_progress`, or `waiting`.
- `reply`: terminal `completed`, `failed`, `blocked`, `cancelled`, or `superseded`.
- `control`: coordinator `cancel` or `close`.
- `notice`: informational, no reply.

Requests may contain `supersedes_request_id`. Control frames contain
`target_request_id`; close controls also contain a final coordinator outcome.

Artifacts contain only:

```text
path
sha256
bytes
```

The helper hashes a shared workspace file but never sends its contents.

## Quiet mode

For independent sessions, prefer:

```text
request
-> terminal reply
-> coordinator close
```

Do not send a routine acknowledgement. Every progress frame is another native queued
message that may later appear as a raw user turn.

Use `--progress-policy on-change` only when a long task genuinely needs a state-change
update. An old V1 or early V2 request without this field retains legacy
`on-change` behavior for compatibility.

## Lifecycle states returned by `check`

| Status | Terminal | Meaning |
|---|---:|---|
| `not_observed` | no | No matching native frame is visible. |
| `queued` | no | Request is present only in the native queue. |
| `request_observed` | no | Peer history contains the request. |
| `notice_queued` | no | No-reply notice is queued. |
| `notice_observed` | yes | Peer history contains the no-reply notice. |
| `acknowledged` | no | Peer acknowledged the request. |
| `in_progress` | no | Peer reported active work. |
| `waiting` | no | Peer reported a concrete wait. |
| `supersede_queued` | no | A replacement exists but is not yet observed. Old replies are stale. |
| `superseded` | yes | Peer observed the replacement. |
| `cancel_queued` | no | Cancellation is queued but not observed. |
| `cancel_observed` | no | Peer observed cancellation; await terminal cancellation or authoritative state. |
| `cancelled` | yes | Peer returned terminal cancellation. |
| `completed` | yes | Effective terminal success/Handoff. |
| `failed` | yes | Effective terminal failure. |
| `blocked` | yes | Effective terminal blocker. |
| `terminal_conflict` | yes | A later terminal reply conflicts with the first; review required. |
| `closed` | yes | Coordinator recorded the outcome; later replies are stale. |

`effective=false` means the terminal body must not be treated as current work.
`stale_reply_count` reports delayed/obsolete terminal replies without exposing their
body. `peer_last_turn` is diagnostic only and can remain `inProgress` briefly after a
terminal frame.

The first terminal reply is retained. `duplicate_terminal_count` reports exact
repeats. `terminal_conflict_count` reports later replies whose status, body, metadata,
or artifacts differ; they never overwrite the first reply.

## Revision discipline

Use one stable `scope_id` for a Work Package or bounded task. Increment or otherwise
change `revision` whenever its accepted interface, file hashes, or success criteria
change.

When replacing work:

1. Send revision N+1 with `--supersedes` revision N's request ID.
2. Check revision N until it is `superseded`, or independently verify the old turn no
   longer owns side effects.
3. Review only artifacts matching revision N+1.
4. Close both lifecycle records after the formal project decision.

Do not ask a reviewer to choose between two hashes. Freeze one revision before review.
When current native state already shows an active request for the same peer and
`scope_id`, the helper rejects another unless it explicitly supersedes the current
request and keeps the same scope. This is a sequential safety check, not a
cross-process lock.

## Concise Handoffs

A terminal reply should contain:

```text
terminal result
scope/revision
changed paths or artifact pointer
focused/full verification summary
external-access statement
```

Put detailed commands, evidence matrices, and long findings in an authorized shared
document. Attach it with `--artifact`; the frame carries its relative path, SHA-256,
and size. This keeps delayed native messages small and independently verifiable.

## Cancellation and close

Cancellation is cooperative because native queueing cannot interrupt an executing
turn. The receiver should check for controls before:

- external API or hardware use;
- broad or destructive filesystem changes;
- final publication;
- terminal reply after long work.

Ordering is explicit:

- a terminal reply sent before `cancel` remains terminal and the late cancellation is
  ignored;
- after `cancel` is sent, only a terminal `cancelled` reply completes cancellation;
  other later replies are stale.

Receiver-side preflight:

```text
python -B scripts/agent_comm.py check \
  --peer COORDINATOR_UUID --request-id REQUEST_ID --direction inbound
```

The default `auto` direction detects ordinary inbound or outbound exchanges. If both
directions reuse one request ID, it fails closed and requires an explicit direction.

`close` is a coordinator decision after the formal system records the outcome. It
does not mutate project state by itself; it marks later native replies stale.

The helper cannot retract a message already accepted by the native queue or suppress
its eventual UI projection. It prevents stale processing and keeps the effective
reply hidden after close/supersession.

## Troubleshooting

- `send_uncertain`: inspect the same request; never resend automatically.
- If an intentional replay is necessary after inspection, reuse both `request_id` and
  `message_id` with identical content. A visible existing copy becomes
  `already_present` without another queue send.
- `supersede_queued`: replacement has not reached visible history.
- terminal reply plus peer `inProgress`: trust the lifecycle frame, but verify
  artifacts before Acceptance.
- duplicate queue/history entries: the helper deduplicates the logical frame and
  reports all native locations.
- missing peer: resolve exact UUID and workspace; archived sessions are never
  reactivated automatically.
