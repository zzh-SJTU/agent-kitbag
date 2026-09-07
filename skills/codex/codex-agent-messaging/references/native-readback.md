# Local native-store readback

Use this fallback for local sessions when a native directory/readback tool is not
exposed. The helper is intentionally small and read-only apart from invoking
`codex queue` to send messages.

Verified environment: Windows, Codex CLI 0.153.4, `multi_agent_v2` enabled. Independent
sessions completed request/progress/reply, supersession, close, and legacy V1
round-trip checks. This demonstrates this installation; it does not guarantee the same
private storage layout in every future version.

## Forward-test evidence — 2026-09-06

Real independent sessions in one shared workspace verified:

- V2 request/reply with separate stable logical and native message IDs;
- exact request replay returned `already_present` without another queue send;
- a second active same-scope request was rejected;
- revision 2 made revision 1 terminal `superseded` and hid its late reply;
- cancel moved through observation to terminal `cancelled`, with later incompatible
  replies stale;
- conflicting terminal replies preserved the first and returned
  `terminal_conflict`;
- coordinator `close` hid terminal bodies and made later replies stale;
- no-reply notice became terminal `notice_observed`;
- an existing V1 exchange and early V2 frames without `message_id` remain readable.

All tasks were communication-only and made no project, credential, API, or hardware
change.

The helper uses the existing `CODEX_HOME`, falling back to `~/.codex` when unset:

| Store | Fields used |
|---|---|
| `state_*.sqlite` | `threads.id`, `name`, `cwd`, `archived` |
| `queue_*.sqlite` | `queued_items.id`, `thread_id`, `payload_json` |
| `thread_history_*.sqlite` | Visible `userMessage` and `commandExecution` items; turn IDs and status |

Connections use `mode=ro` and `PRAGMA query_only=ON`. Do not use `immutable=1` for
live stores: current messages can still be in WAL. Do not remove WAL/SHM files.
No auth/configuration tables or private reasoning items are queried. Reply lookup
is restricted to the two selected threads and the supplied request ID.

The queue entry may disappear as Codex consumes it. The corresponding visible
history item can have a different item ID, so correlation uses the message frame's
request ID and both endpoint UUIDs. A short queue-to-history projection delay can
produce an interim `not_observed` result. Recheck the same request instead of
resending it. A currently `inProgress` turn identifies a live turn; an old turn,
queue entry, or timestamp alone is not proof of current execution.

The helper validates required table columns and stops on an incompatible schema.
Do not migrate or repair Codex's stores to make this helper work. Inspect current
native capabilities and adapt the read-only queries separately when necessary.

Messages use a first-line `CODEX_AGENT_MESSAGE` JSON frame. The helper emits
`codex-agent-message/2` and reads both V1 and V2.

V2 adds lifecycle metadata: stable logical `message_id`, `scope_id`, `revision`,
`sent_at_utc`, progress/terminal status, supersession, cancel/close controls, and
optional artifact path/hash/size. Old V2 frames without `message_id` remain readable.
Queue/history copies and intentional exact replays are collapsed by `message_id`;
conflicting reuse fails closed.

No native metadata records are marked read or consumed. Lifecycle close/supersede
state is represented by ordinary native message frames and derived at readback time;
the helper creates no side database or processed-message journal.

Because readback is intentionally query-only, the helper cannot delete, retract, or
hide a native queue/history item. A closed or superseded raw frame may still be
displayed by Codex later; callers must treat the derived lifecycle state as the task
authority and avoid repeating stale content.

Run the helper's isolated tests with:

```text
python -B -m unittest discover -s /path/to/skill/scripts -p test_agent_comm.py -v
```

Tests use disposable native-store-shaped fixtures and a mocked queue process;
they do not send messages. Real communication checks must additionally verify a
peer-generated progress/terminal reply and lifecycle closure, with user authorization
for that communication scope.
