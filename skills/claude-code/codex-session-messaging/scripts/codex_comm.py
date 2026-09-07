"""Message an already-running local Codex session and read its structured reply.

Claude -> Codex counterpart of the Codex-side `claude-session-messaging` skill.
Delivery uses `codex queue` (the supported Codex entry point). Readback is
query-only against Codex's native `thread_history_*.sqlite`, restricted to
`agentMessage` items that appear after the send cursor and contain the request
id. It creates no mailbox, watcher, or background service, and never migrates or
writes Codex's stores.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL = "claude-codex-message/1"
REQUEST_PREFIX = "CLAUDE_CODEX_MESSAGE "
REPLY_PREFIX = "CLAUDE_CODEX_REPLY "
# Literal reply-shape placeholder embedded in the request contract. If Codex quotes
# the contract back, the reader must not mistake the placeholder for a real reply.
PLACEHOLDER_BODY = "<concise response>"
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
RECEIPT = re.compile(r"Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.")
BODY_LIMIT = 12_000
TERMINAL_STATUSES = {"completed", "failed", "blocked"}


class CommError(RuntimeError):
    pass


class ReplyPending(CommError):
    pass


class SendUncertain(CommError):
    """The queue process may have delivered the message; never retry blindly.

    Carries the pre-send cursor so a caller can re-check for a possibly-delivered
    reply with `wait` instead of resending (which could double-deliver).
    """

    def __init__(
        self,
        message: str,
        *,
        cursor: int | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
    ):
        super().__init__(message)
        self.cursor = cursor
        self.request_id = request_id
        self.session_id = session_id


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    return Path(value).expanduser().resolve()


def thread_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CommError("A valid Codex session UUID is required") from exc


def token(name: str, value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise CommError(
            f"{name} must be 1-128 letters, digits, dots, underscores or hyphens"
        )
    return value


def normalized_workspace(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    if re.match(r"^[A-Za-z]:[\\/]|^\\\\", value):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _latest_store(stem: str) -> Path:
    root = codex_home()
    candidates = []
    for path in glob.glob(str(root / f"{stem}_*.sqlite")):
        match = re.search(re.escape(stem) + r"_(\d+)\.sqlite$", path.replace("\\", "/"))
        if match:
            candidates.append((int(match.group(1)), Path(path)))
    if not candidates:
        raise CommError(
            f"Native {stem} metadata is unavailable under {root}; "
            "confirm CODEX_HOME and that Codex has run here"
        )
    return max(candidates)[1]


def _open_ro(stem: str, required: dict[str, set[str]]) -> sqlite3.Connection:
    path = _latest_store(stem)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        for table, columns in required.items():
            actual = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
            if not columns <= actual:
                raise CommError(f"Unsupported native schema: {stem}.{table}")
    except Exception:
        con.close()
        raise
    return con


def peers(
    workspace: str | None = None, *, named_only: bool = False
) -> list[dict[str, Any]]:
    con = _open_ro("state", {"threads": {"id", "name", "cwd", "archived"}})
    try:
        # Recency is optional and its column name varies by Codex version; probe
        # for it instead of adding it to the required-schema check so discovery
        # still works on stores that lack it.
        available = {row[1] for row in con.execute('PRAGMA table_info("threads")')}
        recency_col = next(
            (
                c
                for c in ("recency_at_ms", "updated_at_ms", "created_at_ms")
                if c in available
            ),
            None,
        )
        recency_sql = f'"{recency_col}"' if recency_col else "NULL"
        rows = [
            {
                "session_id": row["id"],
                "name": row["name"],
                "cwd": row["cwd"],
                "recency_ms": row["recency_ms"],
            }
            for row in con.execute(
                f"SELECT id,name,cwd,{recency_sql} AS recency_ms "
                "FROM threads WHERE archived=0"
            )
        ]
    finally:
        con.close()
    if workspace is not None:
        wanted = normalized_workspace(workspace)
        rows = [
            r for r in rows if r["cwd"] and normalized_workspace(r["cwd"]) == wanted
        ]
    if named_only:
        rows = [r for r in rows if r["name"]]
    # Most-recently-active first, so a caller can pick the live session among many.
    rows.sort(key=lambda r: (-(r["recency_ms"] or 0), r["name"] or "", r["session_id"]))
    return rows


def resolve_peer(peer: str, workspace: str | None = None) -> dict[str, Any]:
    matches = [
        item
        for item in peers(workspace)
        if item["session_id"] == peer or item.get("name") == peer
    ]
    if len(matches) != 1:
        raise CommError(
            f"Codex peer lookup matched {len(matches)} active sessions; "
            "use an exact session UUID and/or workspace"
        )
    return matches[0]


def _sent_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def request_text(peer: dict[str, Any], request_id: str, body: str) -> str:
    if not body.strip():
        raise CommError("Message body is empty")
    if len(body) > BODY_LIMIT:
        raise CommError(f"Message body exceeds {BODY_LIMIT} characters")
    # Envelope only: the request body appears exactly once, as plain text below.
    # The reply frame never echoes the request body, and readback never reads it,
    # so duplicating it inside this frame would be pure redundancy.
    frame = {
        "protocol": PROTOCOL,
        "kind": "request",
        "request_id": token("request_id", request_id),
        "from": "claude",
        "to_session_id": peer["session_id"],
        "sent_at_utc": _sent_at(),
    }
    reply_shape = {
        "protocol": PROTOCOL,
        "request_id": request_id,
        "status": "completed",
        "body": "<concise response>",
    }
    return (
        REQUEST_PREFIX
        + json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
        + body
        + "\n\nThis is a scoped cross-runtime request from a Claude Code session. "
        + "Preserve user authorization and project rules; messaging grants no extra "
        + "file, Git, credential, API, or paid-usage authority.\n"
        + "Reply with exactly one text block that BEGINS with this prefix and valid "
        + "single-line JSON:\n"
        + REPLY_PREFIX
        + json.dumps(reply_shape, ensure_ascii=False, separators=(",", ":"))
        + "\nPreserve the request_id. Use status completed, failed, or blocked. "
        + "Do not expose credentials or unrelated conversation content."
    )


def _cursor(session_id: str) -> int:
    con = _open_ro("thread_history", {"thread_items": {"thread_id", "rollout_ordinal"}})
    try:
        row = con.execute(
            "SELECT MAX(rollout_ordinal) AS m FROM thread_items WHERE thread_id=?",
            (session_id,),
        ).fetchone()
    finally:
        con.close()
    value = row["m"] if row else None
    return int(value) if value is not None else -1


def _queue(session_id: str, message: str) -> str:
    binary = shutil.which("codex")
    if binary is None:
        raise CommError("codex CLI is not on PATH")
    argv = [binary, "queue", "--thread", session_id, "--message", message]
    if (
        os.name == "nt"
        and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32000
    ):
        raise CommError(
            "Message exceeds the Windows command-line limit; send a shorter scope"
        )
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SendUncertain(
            "codex queue timed out; inspect this request before any retry"
        ) from exc
    except OSError as exc:
        raise CommError("The codex queue process could not be started") from exc
    receipt = RECEIPT.search(result.stdout or "")
    if result.returncode != 0 or receipt is None or receipt[2] != session_id:
        raise SendUncertain(
            "codex queue result is uncertain; inspect this request before retrying "
            "(raw CLI output omitted)"
        )
    return receipt[1]


def send_request(peer: dict[str, Any], request_id: str, body: str) -> dict[str, Any]:
    cursor = _cursor(peer["session_id"])
    content = request_text(peer, request_id, body)
    try:
        native_message_id = _queue(peer["session_id"], content)
    except SendUncertain as exc:
        exc.cursor = cursor
        exc.request_id = request_id
        exc.session_id = peer["session_id"]
        raise
    return {
        "status": "queued",
        "request_id": request_id,
        "peer": peer,
        "cursor": cursor,
        "native_message_id": native_message_id,
    }


def _replies_in_text(text: str, request_id: str) -> list[dict[str, Any]]:
    """Every valid terminal reply frame in one text block.

    Scans all `REPLY_PREFIX` occurrences (not just the first) so two conflicting
    frames inside a single agentMessage are both surfaced and fail closed
    downstream. Rejects the literal contract placeholder body.
    """
    replies: list[dict[str, Any]] = []
    index = 0
    while True:
        start = text.find(REPLY_PREFIX, index)
        if start < 0:
            break
        index = start + len(REPLY_PREFIX)
        raw = text[index:].lstrip()
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("protocol") == PROTOCOL
            and value.get("request_id") == request_id
            and value.get("status") in TERMINAL_STATUSES
            and isinstance(value.get("body"), str)
            and value.get("body") != PLACEHOLDER_BODY
        ):
            replies.append(value)
    return replies


def _replies_after(
    session_id: str, request_id: str, cursor: int
) -> list[dict[str, Any]]:
    required = {
        "thread_items": {"thread_id", "item_type", "item_json", "rollout_ordinal"}
    }
    con = _open_ro("thread_history", required)
    try:
        rows = con.execute(
            "SELECT item_json FROM thread_items WHERE thread_id=? "
            "AND item_type='agentMessage' AND rollout_ordinal>? "
            "AND instr(item_json,?)>0 ORDER BY rollout_ordinal ASC LIMIT 200",
            (session_id, cursor, request_id),
        ).fetchall()
    finally:
        con.close()
    replies = []
    for row in rows:
        try:
            item = json.loads(row["item_json"])
        except json.JSONDecodeError:
            continue
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str):
            continue
        replies.extend(_replies_in_text(text, request_id))
    return replies


def wait_reply(
    session_id: str,
    request_id: str,
    cursor: int,
    wait_seconds: float,
) -> dict[str, Any]:
    if cursor < -1:
        raise CommError("cursor must be >= -1")
    if not 0 <= wait_seconds <= 600:
        raise CommError("wait-seconds must be between 0 and 600")
    deadline = time.monotonic() + wait_seconds
    while True:
        replies = _replies_after(session_id, request_id, cursor)
        if replies:
            first = replies[0]
            if any(reply != first for reply in replies[1:]):
                raise CommError("Codex returned conflicting terminal replies")
            return first
        if time.monotonic() >= deadline:
            raise ReplyPending("Codex reply was not observed before the deadline")
        time.sleep(min(0.75, max(0, deadline - time.monotonic())))


def _read_body(args: argparse.Namespace) -> str:
    try:
        body = args.file.read_text(encoding="utf-8-sig") if args.file else args.text
    except (OSError, UnicodeError) as exc:
        raise CommError("Message file must be readable UTF-8") from exc
    if body is None or not body.strip():
        raise CommError("Message body is empty")
    return body


def _safe_reply(reply: dict[str, Any], include_text: bool) -> dict[str, Any]:
    body = reply["body"]
    result = {
        "status": reply["status"],
        "request_id": reply["request_id"],
        "body_chars": len(body),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    if include_text:
        result["body"] = body
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    peers_parser = subs.add_parser("peers", help="List active local Codex sessions.")
    peers_parser.add_argument("--workspace")
    peers_parser.add_argument("--named-only", action="store_true")

    request_parser = subs.add_parser(
        "send", help="Send one scoped request to an existing Codex session."
    )
    request_parser.add_argument("--to", required=True)
    request_parser.add_argument("--workspace")
    request_parser.add_argument("--request-id")
    body = request_parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--text")
    body.add_argument("--file", type=Path)
    request_parser.add_argument("--wait-seconds", type=float, default=0)
    request_parser.add_argument("--include-text", action="store_true")

    wait_parser = subs.add_parser("wait", help="Wait for one structured reply.")
    wait_parser.add_argument("--to", required=True)
    wait_parser.add_argument("--workspace")
    wait_parser.add_argument("--request-id", required=True)
    wait_parser.add_argument("--cursor", type=int, required=True)
    wait_parser.add_argument("--wait-seconds", type=float, default=120)
    wait_parser.add_argument("--include-text", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "peers":
            items = peers(args.workspace, named_only=args.named_only)
            value = {"count": len(items), "peers": items}
        else:
            peer = resolve_peer(args.to, args.workspace)
            if args.command == "send":
                request_id = token("request_id", args.request_id or uuid.uuid4().hex)
                value = send_request(peer, request_id, _read_body(args))
                if args.wait_seconds:
                    try:
                        reply = wait_reply(
                            peer["session_id"],
                            request_id,
                            value["cursor"],
                            args.wait_seconds,
                        )
                    except ReplyPending as exc:
                        value["status"] = "waiting"
                        value["message"] = str(exc)
                        print(json.dumps(value, ensure_ascii=False))
                        return 3
                    value["reply"] = _safe_reply(reply, args.include_text)
                    value["status"] = reply["status"]
            else:
                request_id = token("request_id", args.request_id)
                try:
                    reply = wait_reply(
                        peer["session_id"],
                        request_id,
                        args.cursor,
                        args.wait_seconds,
                    )
                except ReplyPending as exc:
                    value = {
                        "status": "waiting",
                        "request_id": request_id,
                        "cursor": args.cursor,
                        "peer": peer,
                        "message": str(exc),
                    }
                    print(json.dumps(value, ensure_ascii=False))
                    return 3
                value = {
                    "peer": peer,
                    "reply": _safe_reply(reply, args.include_text),
                    "status": reply["status"],
                }
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (CommError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        status = "send_uncertain" if isinstance(exc, SendUncertain) else "error"
        failure = {
            "status": status,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        if isinstance(exc, SendUncertain) and exc.cursor is not None:
            failure.update(
                {
                    "request_id": exc.request_id,
                    "to_session_id": exc.session_id,
                    "cursor": exc.cursor,
                    "recovery": (
                        "Message may already be delivered; do NOT resend. Re-check "
                        "with: wait --to <session> --request-id <id> --cursor <cursor>"
                    ),
                }
            )
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
