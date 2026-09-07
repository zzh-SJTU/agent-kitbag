# Local Claude peer protocol

Verified locally with Claude Code 2.1.263 on Windows.

Discovery uses:

```text
claude agents --json --cwd WORKSPACE
```

An active session publishes:

```text
sessionId
pid
cwd
name
status
messagingSocketPath
peerProtocol
peerFeatures
```

The Windows inbox is a local named pipe shaped like:

```text
\\.\pipe\LOCAL\cc-msg-<32 hex>
```

The matching process key file contains a 32-hex `peerToken` plus process-start
identity. The helper verifies those fields against the live registry record and uses
the token only in the child process environment.

The peer inbox accepts newline-delimited JSON:

```text
{"type":"auth","token":"<peer token>"}
{"type":"user","msg_id":"<request id>","from":"codex:<session id>","message":{"role":"user","content":"..."}}
```

`msg_id` equals the bridge request ID so Claude's inbox can recognize a duplicate
transport submission. The human-readable request body appears once, inside the
`CLAUDE_AGENT_MESSAGE` frame.

If the named-pipe client times out or exits unclearly, the helper reports
`send_uncertain` with the pre-send cursor, request ID, session ID, and log path.
Callers re-check with `wait`; they do not resend.

The request asks Claude to emit one `CLAUDE_AGENT_REPLY` JSON object at the beginning
of a text block. Real agents can still add a short preamble, so the reader tolerates
that deviation while accepting only valid matching replies, ignoring the prompt's
literal placeholder, and failing closed on conflicts. The helper records the session
JSONL byte offset before sending and examines only subsequently appended assistant
text blocks for the matching request ID.

An unidentified Codex peer does not attest a Claude permission mode. Claude can place
the message in its held queue and show the user a preview. After explicit approval,
the same message is released to Claude's queue and the helper observes the reply.
The bridge intentionally does not set `from_mode` or modify `crossSessionInbound`.

Limitations:

- A request may consume the target Claude provider's quota.
- Held messages require target-side user approval unless the user independently
  configures Claude to accept them.
- The tool does not interrupt, cancel, start, resume, or delete Claude sessions.
- It does not create a Codex-compatible inbox for unsolicited Claude messages.
- Claude CLI storage and peer protocol can change; fail closed when registry, key,
  pipe, or response shape no longer matches.

Official references:

- Claude Code cross-session messaging:
  `https://code.claude.com/docs/en/cross-session-messaging`
- Claude Agent SDK sessions:
  `https://platform.claude.com/docs/en/agent-sdk/sessions`
