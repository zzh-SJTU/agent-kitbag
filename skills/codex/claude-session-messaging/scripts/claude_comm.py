"""Message an already-running local Claude Code session through its peer inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL = "codex-claude-message/1"
REQUEST_PREFIX = "CLAUDE_AGENT_MESSAGE "
REPLY_PREFIX = "CLAUDE_AGENT_REPLY "
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
PEER_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
WINDOWS_PIPE = re.compile(
    r"^\\\\[.?]\\pipe\\(?:LOCAL\\)?cc-msg-[0-9a-f]{32}$", re.IGNORECASE
)
BODY_LIMIT = 8_000
TERMINAL_STATUSES = {"completed", "failed", "blocked"}


class CommError(RuntimeError):
    pass


class ReplyPending(CommError):
    pass


class SendUncertain(CommError):
    cursor: int | None = None
    request_id: str | None = None
    session_id: str | None = None
    log: str | None = None


def claude_home() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(value).resolve()


def token(name: str, value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise CommError(
            f"{name} must be 1-128 letters, digits, dots, underscores or hyphens"
        )
    return value


def _run_json(argv: list[str], *, timeout: float = 15) -> Any:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommError("Claude session discovery timed out") from exc
    except OSError as exc:
        raise CommError("Claude Code could not be started") from exc
    if result.returncode != 0:
        raise CommError("Claude session discovery failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CommError("Claude session discovery returned invalid JSON") from exc


def peers(workspace: str | None = None) -> list[dict[str, Any]]:
    binary = shutil.which("claude")
    if binary is None:
        raise CommError("Claude Code is not on PATH")
    argv = [binary, "agents", "--json"]
    if workspace:
        argv.extend(["--cwd", str(Path(workspace).resolve())])
    value = _run_json(argv)
    if not isinstance(value, list):
        raise CommError("Claude session discovery returned an invalid result")
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        session_id = item.get("sessionId")
        if not isinstance(session_id, str):
            continue
        result.append(
            {
                "session_id": session_id,
                "name": item.get("name"),
                "cwd": item.get("cwd"),
                "status": item.get("status"),
                "kind": item.get("kind"),
                "pid": item.get("pid"),
            }
        )
    return result


def resolve_peer(peer: str, workspace: str | None = None) -> dict[str, Any]:
    matches = [
        item
        for item in peers(workspace)
        if item["session_id"] == peer or item.get("name") == peer
    ]
    if len(matches) != 1:
        raise CommError(
            f"Claude peer lookup matched {len(matches)} active sessions; "
            "use an exact session ID/workspace"
        )
    return matches[0]


def _registry_record(peer: dict[str, Any]) -> dict[str, Any]:
    pid = peer.get("pid")
    if type(pid) is not int or pid <= 0:
        raise CommError("Claude peer has no valid process ID")
    path = claude_home() / "sessions" / f"{pid}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommError("Claude peer registry record is unavailable") from exc
    if (
        not isinstance(value, dict)
        or value.get("sessionId") != peer["session_id"]
        or value.get("pid") != pid
    ):
        raise CommError("Claude peer registry identity changed")
    if (
        isinstance(peer.get("cwd"), str)
        and isinstance(value.get("cwd"), str)
        and Path(value["cwd"]).resolve() != Path(peer["cwd"]).resolve()
    ):
        raise CommError("Claude peer workspace changed")
    socket_path = value.get("messagingSocketPath")
    if not isinstance(socket_path, str) or not WINDOWS_PIPE.fullmatch(socket_path):
        raise CommError("Claude peer does not expose a supported local inbox")
    if value.get("peerProtocol") != 1:
        raise CommError("Claude peer protocol is unsupported")
    return value


def _peer_token(record: dict[str, Any]) -> str:
    pid = record["pid"]
    candidates = list((claude_home() / "sessions").glob(f"{pid}.*.key"))
    if len(candidates) != 1:
        raise CommError("Claude peer authentication record is unavailable")
    try:
        value = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommError("Claude peer authentication record is invalid") from exc
    peer_token = value.get("peerToken")
    if not isinstance(peer_token, str) or not PEER_TOKEN.fullmatch(peer_token):
        raise CommError("Claude peer authentication record is invalid")
    registry_start = record.get("procStartFt")
    if registry_start is not None and str(value.get("procStartFt")) != str(
        registry_start
    ):
        raise CommError("Claude peer authentication identity changed")
    if str(value.get("pidDomain")) != str(record.get("pidDomain")):
        raise CommError("Claude peer authentication identity changed")
    return peer_token


def _session_log(session_id: str) -> Path:
    matches = list((claude_home() / "projects").glob(f"**/{session_id}.jsonl"))
    if len(matches) != 1:
        raise CommError(f"Claude session log lookup matched {len(matches)} files")
    return matches[0].resolve()


def _node_send(socket_path: str, peer_token: str, frames: list[dict[str, Any]]) -> None:
    node = shutil.which("node")
    if node is None:
        raise CommError("Node.js is required for the Claude local named-pipe client")
    payload = "".join(
        json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        for frame in frames
    )
    script = (
        "const net=require('net');let d='';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data',c=>d+=c);"
        "process.stdin.on('end',()=>{"
        "const c=net.connect(process.argv[1],()=>{"
        "c.write(JSON.stringify({type:'auth',token:process.env.CLAUDE_PEER_TOKEN})"
        "+'\\n'+d);c.end();});"
        "c.setTimeout(5000,()=>{c.destroy();process.exitCode=2});"
        "c.on('error',()=>{process.exitCode=2});});"
    )
    env = os.environ.copy()
    env["CLAUDE_PEER_TOKEN"] = peer_token
    try:
        result = subprocess.run(
            [node, "-e", script, socket_path],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SendUncertain(
            "Claude peer send timed out; inspect this request before any retry"
        ) from exc
    except OSError as exc:
        raise CommError("Claude peer client could not be started") from exc
    if result.returncode != 0:
        raise SendUncertain(
            "Claude peer send was uncertain; inspect this request before any retry"
        )


def probe(peer: dict[str, Any]) -> dict[str, Any]:
    record = _registry_record(peer)
    _node_send(record["messagingSocketPath"], _peer_token(record), [])
    return {
        "status": "reachable",
        "peer": peer,
        "protocol": record["peerProtocol"],
        "features": record.get("peerFeatures", []),
    }


def _sent_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sender_label() -> str:
    identities = [
        os.environ.get("CODEX_THREAD_ID"),
        os.environ.get("CODEX_SESSION_ID"),
    ]
    identity = next((value for value in identities if value), None)
    return f"codex:{identity}" if identity else "codex"


def request_text(
    peer: dict[str, Any],
    request_id: str,
    body: str,
) -> str:
    if not body.strip():
        raise CommError("Message body is empty")
    if len(body) > BODY_LIMIT:
        raise CommError(f"Message body exceeds {BODY_LIMIT} characters")
    frame = {
        "protocol": PROTOCOL,
        "kind": "request",
        "request_id": token("request_id", request_id),
        "from": "codex",
        "to_session_id": peer["session_id"],
        "sent_at_utc": _sent_at(),
        "body": body,
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
        + "\n\nReply with exactly one text block beginning with this prefix and valid "
        + "single-line JSON:\n"
        + REPLY_PREFIX
        + json.dumps(reply_shape, ensure_ascii=False, separators=(",", ":"))
        + "\nPreserve the request_id. Use status completed, failed, or blocked. "
        + "Do not expose credentials or unrelated conversation content."
    )


def send_request(peer: dict[str, Any], request_id: str, body: str) -> dict[str, Any]:
    record = _registry_record(peer)
    log = _session_log(peer["session_id"])
    cursor = log.stat().st_size
    content = request_text(peer, request_id, body)
    try:
        _node_send(
            record["messagingSocketPath"],
            _peer_token(record),
            [
                {
                    "type": "user",
                    "msg_id": request_id,
                    "from": _sender_label(),
                    "message": {"role": "user", "content": content},
                }
            ],
        )
    except SendUncertain as exc:
        exc.cursor = cursor
        exc.request_id = request_id
        exc.session_id = peer["session_id"]
        exc.log = str(log)
        raise
    return {
        "status": "queued",
        "request_id": request_id,
        "peer": peer,
        "cursor": cursor,
        "log": str(log),
    }


def _reply_from_text(text: str, request_id: str) -> dict[str, Any] | None:
    replies = []
    start = 0
    while True:
        index = text.find(REPLY_PREFIX, start)
        if index < 0:
            break
        raw = text[index + len(REPLY_PREFIX) :].lstrip()
        start = index + len(REPLY_PREFIX)
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
            and value.get("body") != "<concise response>"
        ):
            replies.append(value)
    if not replies:
        return None
    first = replies[0]
    if any(reply != first for reply in replies[1:]):
        raise CommError("Claude returned conflicting terminal replies")
    return first


def _replies_from_bytes(data: bytes, request_id: str) -> list[dict[str, Any]]:
    replies = []
    for raw_line in data.splitlines():
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        message = item.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            reply = _reply_from_text(text, request_id)
            if reply is not None:
                replies.append(reply)
    return replies


def wait_reply(
    log: Path,
    request_id: str,
    cursor: int,
    wait_seconds: float,
) -> dict[str, Any]:
    if cursor < 0:
        raise CommError("cursor must be nonnegative")
    if not 0 <= wait_seconds <= 600:
        raise CommError("wait-seconds must be between 0 and 600")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            size = log.stat().st_size
            if size < cursor:
                raise CommError("Claude session log was replaced during the request")
            if size > cursor:
                with log.open("rb") as stream:
                    stream.seek(cursor)
                    replies = _replies_from_bytes(stream.read(), request_id)
                if replies:
                    first = replies[0]
                    if any(reply != first for reply in replies[1:]):
                        raise CommError("Claude returned conflicting terminal replies")
                    return first
        except OSError as exc:
            raise CommError("Claude session log became unavailable") from exc
        if time.monotonic() >= deadline:
            raise ReplyPending("Claude reply was not observed before the deadline")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


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

    peers_parser = subs.add_parser("peers", help="List active local Claude sessions.")
    peers_parser.add_argument("--workspace")

    probe_parser = subs.add_parser("probe", help="Authenticate to a peer inbox.")
    probe_parser.add_argument("--to", required=True)
    probe_parser.add_argument("--workspace")

    request_parser = subs.add_parser(
        "request", help="Send one scoped request to an existing Claude session."
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
    wait_parser.add_argument("--wait-seconds", type=float, default=60)
    wait_parser.add_argument("--include-text", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "peers":
            items = peers(args.workspace)
            value = {"count": len(items), "peers": items}
        else:
            peer = resolve_peer(args.to, args.workspace)
            if args.command == "probe":
                value = probe(peer)
            elif args.command == "request":
                request_id = token("request_id", args.request_id or uuid.uuid4().hex)
                value = send_request(peer, request_id, _read_body(args))
                if args.wait_seconds:
                    try:
                        reply = wait_reply(
                            Path(value["log"]),
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
                log = _session_log(peer["session_id"])
                request_id = token("request_id", args.request_id)
                try:
                    reply = wait_reply(
                        log,
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
    except (CommError, OSError, json.JSONDecodeError) as exc:
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
                    "log": exc.log,
                    "recovery": (
                        "Message may already be delivered; do not resend. "
                        "Re-check with wait using the same request_id and cursor."
                    ),
                }
            )
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
