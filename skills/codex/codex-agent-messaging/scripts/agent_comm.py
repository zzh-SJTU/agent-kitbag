"""Native Codex session messaging with correlated request lifecycle readback."""

from __future__ import annotations

import argparse
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
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIX = "CODEX_AGENT_MESSAGE "
PROTOCOL_V1 = "codex-agent-message/1"
PROTOCOL = "codex-agent-message/2"
SUPPORTED_PROTOCOLS = {PROTOCOL_V1, PROTOCOL}
RECEIPT = re.compile(r"Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})\.")
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
V2_KINDS = {"request", "notice", "progress", "reply", "control"}
PROGRESS_STATUSES = {"acknowledged", "in_progress", "waiting"}
PROGRESS_POLICIES = {"none", "on-change"}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "superseded",
}
CONTROL_ACTIONS = {"cancel", "close"}
CLOSE_OUTCOMES = {"accepted", "completed", "failed", "withdrawn"}
BODY_LIMITS = {
    "request": 12_000,
    "notice": 4_000,
    "progress": 1_200,
    "reply": 4_000,
    "control": 1_200,
}


class CommError(RuntimeError):
    pass


class SendUncertain(CommError):
    """The process may have enqueued the message; never retry automatically."""

    def __init__(
        self,
        message: str,
        *,
        message_id: str | None = None,
        request_id: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        kind: str | None = None,
    ):
        super().__init__(message)
        self.message_id = message_id
        self.request_id = request_id
        self.sender = sender
        self.recipient = recipient
        self.kind = kind


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    return Path(value).expanduser().resolve()


def thread_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise CommError("A valid native session UUID is required") from exc


def current_thread() -> str:
    values = [os.environ.get(key) for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID")]
    identities = {thread_uuid(value) for value in values if value}
    if len(identities) != 1:
        raise CommError(
            "Current session identity is missing or its environment IDs disagree"
        )
    return identities.pop()


def normalized_workspace(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    if re.match(r"^[A-Za-z]:[\\/]|^\\\\", value):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def token(name: str, value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise CommError(
            f"{name} must be 1-128 letters, digits, dots, underscores or hyphens"
        )
    return value


def request_id(value: str) -> str:
    return token("request_id", value)


def strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def _artifacts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CommError("artifacts must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise CommError("artifact metadata must be an object")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("bytes")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
            or type(size) is not int
            or size < 0
        ):
            raise CommError("artifact metadata is invalid")
        result.append({"path": path, "sha256": digest, "bytes": size})
    return result


def _normalized_frame(frame: dict[str, Any]) -> dict[str, Any]:
    protocol = frame.get("protocol")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise CommError("unsupported messaging protocol")
    kind = frame.get("kind")
    allowed = {"request", "notice", "reply"} if protocol == PROTOCOL_V1 else V2_KINDS
    if not isinstance(kind, str) or kind not in allowed:
        raise CommError("invalid message kind")
    correlation = request_id(frame["request_id"])
    sender = thread_uuid(frame["from"])
    recipient = thread_uuid(frame["to"])
    body = frame.get("body")
    if not isinstance(body, str):
        raise CommError("message body must be text")
    result = dict(frame)
    result.update(
        {
            "protocol": protocol,
            "kind": kind,
            "request_id": correlation,
            "from": sender,
            "to": recipient,
            "body": body,
        }
    )
    if protocol == PROTOCOL_V1:
        if kind == "reply" and frame.get("expects_reply") is not False:
            raise CommError("legacy reply cannot request another reply")
        result.setdefault("scope_id", correlation)
        result.setdefault("revision", "legacy")
        result.setdefault("artifacts", [])
        result.setdefault("sent_at_utc", None)
        result.setdefault("message_id", None)
        if kind == "request":
            result.setdefault("progress_policy", "on-change")
        if kind == "reply":
            result.setdefault("status", "completed")
        return result

    expects = frame.get("expects_reply")
    if expects is not (kind == "request"):
        raise CommError("expects_reply does not match message kind")
    reply_to = frame.get("reply_to")
    if kind == "request":
        if reply_to != sender:
            raise CommError("request reply_to must equal sender")
    elif reply_to is not None:
        raise CommError("non-request reply_to must be null")
    sent_at = frame.get("sent_at_utc")
    if not isinstance(sent_at, str) or not sent_at.endswith("Z"):
        raise CommError("sent_at_utc is invalid")
    logical_message_id = frame.get("message_id")
    if logical_message_id is not None:
        logical_message_id = token("message_id", logical_message_id)
    scope = token("scope_id", frame.get("scope_id", correlation))
    revision = token("revision", frame.get("revision", "1"))
    result.update(
        {
            "scope_id": scope,
            "revision": revision,
            "message_id": logical_message_id,
            "artifacts": _artifacts(frame.get("artifacts")),
            "sent_at_utc": sent_at,
        }
    )
    if kind == "request":
        progress_policy = frame.get("progress_policy", "on-change")
        if progress_policy not in PROGRESS_POLICIES:
            raise CommError("invalid progress policy")
        result["progress_policy"] = progress_policy
    elif "progress_policy" in frame:
        raise CommError("progress_policy is request-only")
    supersedes = frame.get("supersedes_request_id")
    if supersedes is not None:
        result["supersedes_request_id"] = request_id(supersedes)
    if kind == "progress":
        status = frame.get("status")
        if status not in PROGRESS_STATUSES:
            raise CommError("invalid progress status")
    elif kind == "reply":
        status = frame.get("status")
        if status not in TERMINAL_STATUSES:
            raise CommError("invalid terminal status")
    elif kind == "control":
        action = frame.get("action")
        if action not in CONTROL_ACTIONS:
            raise CommError("invalid control action")
        if frame.get("target_request_id") != correlation:
            raise CommError("control target does not match request_id")
        if action == "close" and frame.get("outcome") not in CLOSE_OUTCOMES:
            raise CommError("invalid close outcome")
        replacement = frame.get("replacement_request_id")
        if replacement is not None:
            result["replacement_request_id"] = request_id(replacement)
    return result


def frames(value: Any) -> Iterable[dict[str, Any]]:
    for text in strings(value):
        first = text.split("\n", 1)[0]
        if not first.startswith(PREFIX):
            continue
        try:
            raw = json.loads(first[len(PREFIX) :])
            if not isinstance(raw, dict):
                continue
            yield _normalized_frame(raw)
        except (json.JSONDecodeError, CommError, KeyError, TypeError):
            continue


def _frame_payload_key(
    frame: dict[str, Any], *, ignore_message_id: bool = False
) -> str:
    value = dict(frame)
    value.pop("sent_at_utc", None)
    if ignore_message_id:
        value.pop("message_id", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _record_key(record: dict[str, Any]) -> str:
    frame = record["frame"]
    logical_message_id = frame.get("message_id")
    if logical_message_id:
        return "message:" + logical_message_id
    return "frame:" + json.dumps(frame, ensure_ascii=False, sort_keys=True)


def _record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    frame = record["frame"]
    return (
        frame.get("sent_at_utc") or "",
        record.get("rollout_ordinal", -1),
        1 if record.get("location") == "visible_history" else 0,
        record.get("native_item_id", ""),
    )


def _dedupe(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "frame": record["frame"],
                "locations": [],
                "native_item_ids": [],
                "turn_ids": [],
                "rollout_ordinal": record.get("rollout_ordinal", -1),
            }
            grouped[key] = entry
        elif record["frame"].get("message_id") and _frame_payload_key(
            entry["frame"]
        ) != _frame_payload_key(record["frame"]):
            raise CommError("message_id was reused with conflicting message content")
        if record["location"] not in entry["locations"]:
            entry["locations"].append(record["location"])
        if record["native_item_id"] not in entry["native_item_ids"]:
            entry["native_item_ids"].append(record["native_item_id"])
        turn_id = record.get("turn_id")
        if turn_id and turn_id not in entry["turn_ids"]:
            entry["turn_ids"].append(turn_id)
        entry["rollout_ordinal"] = max(
            entry["rollout_ordinal"], record.get("rollout_ordinal", -1)
        )
    return sorted(grouped.values(), key=_record_sort_key)


def _latest(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(records, key=_record_sort_key) if records else None


class NativeStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    @contextmanager
    def open(self, stem: str, required: dict[str, set[str]]):
        candidates = []
        for path in self.root.glob(stem + "_*.sqlite"):
            match = re.fullmatch(re.escape(stem) + r"_(\d+)\.sqlite", path.name)
            if match:
                candidates.append((int(match[1]), path))
        if not candidates:
            raise CommError(
                f"Native {stem} metadata is unavailable; "
                "use a native directory/readback tool"
            )
        path = max(candidates)[1]
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=ON")
            for table, columns in required.items():
                actual = {
                    row[1] for row in con.execute(f'PRAGMA table_info("{table}")')
                }
                if not columns <= actual:
                    raise CommError(
                        f"Unsupported native metadata schema: {stem}.{table}"
                    )
            yield con
        finally:
            con.close()

    def peers(
        self,
        name: str | None = None,
        workspace: str | None = None,
        *,
        named_only: bool = False,
        exclude: str | None = None,
    ):
        with self.open("state", {"threads": {"id", "name", "cwd", "archived"}}) as con:
            sql = "SELECT id,name,cwd FROM threads WHERE archived=0"
            params: tuple[Any, ...] = ()
            if name is not None:
                try:
                    identifier = thread_uuid(name)
                except CommError:
                    sql += " AND name=?"
                    params = (name,)
                else:
                    sql += " AND id=?"
                    params = (identifier,)
            rows = [dict(row) for row in con.execute(sql + " ORDER BY name,id", params)]
        if workspace is not None:
            wanted = normalized_workspace(workspace)
            rows = [row for row in rows if normalized_workspace(row["cwd"]) == wanted]
        if named_only:
            rows = [row for row in rows if row["name"]]
        if exclude is not None:
            rows = [row for row in rows if row["id"] != exclude]
        return rows

    def resolve(self, recipient: str, workspace: str | None = None):
        matches = self.peers(recipient, workspace)
        if len(matches) != 1:
            raise CommError(
                f"Recipient lookup matched {len(matches)} unarchived sessions; "
                "use an exact UUID/workspace"
            )
        return matches[0]

    def last_turn(self, thread: str):
        required = {
            "thread_turns": {"thread_id", "turn_id", "status", "rollout_ordinal"}
        }
        with self.open("thread_history", required) as con:
            row = con.execute(
                "SELECT turn_id,status FROM thread_turns WHERE thread_id=? "
                "ORDER BY rollout_ordinal DESC LIMIT 1",
                (thread,),
            ).fetchone()
        return dict(row) if row else None

    def _packets_containing(self, recipient: str, value: str):
        records = []
        with self.open(
            "queue", {"queued_items": {"id", "thread_id", "payload_json"}}
        ) as con:
            for row in con.execute(
                "SELECT id,payload_json FROM queued_items WHERE thread_id=? "
                "AND instr(payload_json,?)>0",
                (recipient, value),
            ):
                for frame in frames(json.loads(row["payload_json"])):
                    records.append(
                        {
                            "location": "native_queue",
                            "native_item_id": row["id"],
                            "frame": frame,
                        }
                    )
        required = {
            "thread_items": {
                "thread_id",
                "turn_id",
                "item_id",
                "item_type",
                "item_json",
                "rollout_ordinal",
            }
        }
        with self.open("thread_history", required) as con:
            for row in con.execute(
                "SELECT item_id,turn_id,item_json,rollout_ordinal FROM thread_items "
                "WHERE thread_id=? AND item_type='userMessage' "
                "AND instr(item_json,?)>0 ORDER BY rollout_ordinal DESC LIMIT 400",
                (recipient, value),
            ):
                for frame in frames(json.loads(row["item_json"])):
                    records.append(
                        {
                            "location": "visible_history",
                            "native_item_id": row["item_id"],
                            "turn_id": row["turn_id"],
                            "rollout_ordinal": row["rollout_ordinal"],
                            "frame": frame,
                        }
                    )
        return records

    def packets(self, recipient: str, correlation: str):
        return self._packets_containing(recipient, correlation)

    def scope_packets(self, recipient: str, scope: str):
        return self._packets_containing(recipient, scope)

    def source_receipts(
        self, sender: str, recipient: str, correlation: str, reply_ids: set[str]
    ):
        required = {
            "thread_items": {
                "thread_id",
                "turn_id",
                "item_id",
                "item_type",
                "item_json",
                "rollout_ordinal",
            }
        }
        results = []
        with self.open("thread_history", required) as con:
            for row in con.execute(
                "SELECT item_id,turn_id,item_json FROM thread_items "
                "WHERE thread_id=? AND item_type='commandExecution' "
                "AND instr(item_json,?)>0 ORDER BY rollout_ordinal DESC LIMIT 40",
                (sender, correlation),
            ):
                item = json.loads(row["item_json"])
                if item.get("exitCode") != 0:
                    continue
                output = item.get("aggregatedOutput") or ""
                matches = []
                for line in output.splitlines():
                    try:
                        receipt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(receipt, dict)
                        and receipt.get("status") == "queued"
                        and receipt.get("kind") == "reply"
                        and receipt.get("request_id") == correlation
                        and receipt.get("from") == sender
                        and receipt.get("to") == recipient
                    ):
                        matches.append(
                            receipt.get("native_message_id")
                            or receipt.get("message_id")
                        )
                for match in RECEIPT.finditer(output):
                    if match[2] == recipient and (
                        match[1] in reply_ids
                        or correlation in (item.get("command") or "")
                    ):
                        matches.append(match[1])
                for message in matches:
                    if isinstance(message, str):
                        results.append(
                            {
                                "message_id": message,
                                "command_item_id": row["item_id"],
                                "turn_id": row["turn_id"],
                                "exit_code": 0,
                            }
                        )
        return results

    def inbound_request(
        self,
        recipient: str,
        correlation: str,
        explicit_sender: str | None = None,
    ) -> dict[str, Any]:
        requests = [
            record
            for record in _dedupe(self.packets(recipient, correlation))
            if record["frame"]["kind"] == "request"
            and record["frame"]["request_id"] == correlation
            and record["frame"]["to"] == recipient
        ]
        if explicit_sender is not None:
            requests = [
                record
                for record in requests
                if record["frame"]["from"] == explicit_sender
            ]
        senders = {record["frame"]["from"] for record in requests}
        if len(senders) != 1:
            raise CommError(
                f"Inbound request lookup matched {len(senders)} senders; "
                "supply the exact peer or verify the request ID"
            )
        latest = _latest(requests)
        assert latest is not None
        return latest["frame"]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_metadata(paths: Iterable[Path], workspace: Path | None = None):
    root = (workspace or Path.cwd()).resolve()
    results = []
    for raw in paths:
        path = raw.expanduser().resolve()
        if not path.is_file():
            raise CommError(f"Artifact is not a file: {raw}")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise CommError("Artifacts must stay within the current workspace") from exc
        if path.name.lower() == ".env" or ".git" in relative.parts:
            raise CommError("Sensitive repository metadata cannot be an artifact")
        results.append(
            {
                "path": relative.as_posix(),
                "sha256": _hash_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return results


def _sent_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def frame_message(
    kind: str,
    sender: str,
    recipient: str,
    correlation: str,
    body: str,
    *,
    status: str | None = None,
    scope_id: str | None = None,
    revision: str | None = None,
    supersedes: str | None = None,
    progress_policy: str | None = None,
    action: str | None = None,
    replacement: str | None = None,
    outcome: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
    protocol: str = PROTOCOL,
) -> str:
    sender = thread_uuid(sender)
    recipient = thread_uuid(recipient)
    correlation = request_id(correlation)
    if protocol == PROTOCOL_V1:
        frame = {
            "protocol": protocol,
            "kind": kind,
            "request_id": correlation,
            "from": sender,
            "to": recipient,
            "reply_to": sender if kind == "request" else None,
            "expects_reply": kind == "request",
            "body": body,
        }
    else:
        if kind not in V2_KINDS:
            raise CommError("invalid message kind")
        frame = {
            "protocol": protocol,
            "kind": kind,
            "request_id": correlation,
            "from": sender,
            "to": recipient,
            "reply_to": sender if kind == "request" else None,
            "expects_reply": kind == "request",
            "message_id": token("message_id", message_id or uuid.uuid4().hex),
            "sent_at_utc": _sent_at(),
            "scope_id": token("scope_id", scope_id or correlation),
            "revision": token("revision", revision or "1"),
            "artifacts": artifacts or [],
            "body": body,
        }
        if kind == "request":
            frame["progress_policy"] = progress_policy or "none"
        elif progress_policy is not None:
            raise CommError("progress_policy is request-only")
        if supersedes is not None:
            frame["supersedes_request_id"] = request_id(supersedes)
        if kind in {"progress", "reply"}:
            frame["status"] = status or (
                "in_progress" if kind == "progress" else "completed"
            )
        if kind == "control":
            frame["action"] = action
            frame["target_request_id"] = correlation
            if replacement is not None:
                frame["replacement_request_id"] = request_id(replacement)
            if outcome is not None:
                frame["outcome"] = outcome
        _normalized_frame(frame)
    text = PREFIX + json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    if kind == "request":
        skill = Path(__file__).resolve().parents[1] / "SKILL.md"
        policy = frame.get("progress_policy", "on-change")
        replacement_text = (
            f" This request supersedes {supersedes}; stop that scope when observed."
            if supersedes
            else ""
        )
        text += (
            "\n\nThis is a scoped request from another Codex session."
            + replacement_text
            + " Preserve user authorization and project rules. "
            + (
                "Do not send acknowledgement or progress messages; return one "
                "terminal reply only. "
                if policy == "none"
                else "Send progress only when state meaningfully changes; never "
                "send a routine acknowledgement. "
            )
            + "Before external actions and before the final reply, check for cancel, "
            "close, or superseding control. Return a concise terminal reply with the "
            "unchanged request_id; put detailed Handoffs in an authorized shared "
            "artifact and attach only its path/hash metadata. Use "
            + str(skill)
            + ". Terminal replies are not acknowledged."
        )
    return text


def _body_limit(kind: str, body: str, allow_long: bool) -> None:
    limit = BODY_LIMITS[kind]
    if len(body) > limit and not allow_long:
        raise CommError(
            f"{kind} body exceeds {limit} characters; use a concise summary plus "
            "--artifact, or pass --allow-long-body intentionally"
        )


def send_native(
    recipient: str,
    message: str,
    *,
    correlation: str,
    sender: str,
    kind: str,
):
    parsed = next(frames({"message": message}), None)
    logical_message_id = parsed.get("message_id") if parsed else None
    binary = shutil.which("codex")
    if binary is None:
        raise CommError("codex CLI is not on PATH; inspect the installed CLI location")
    argv = [binary, "queue", "--thread", recipient, "--message", message]
    if (
        os.name == "nt"
        and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32000
    ):
        raise CommError(
            "Message exceeds the Windows command-line limit; "
            "send a scoped artifact pointer instead"
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
            "Queue command timed out; inspect this request before any retry",
            message_id=logical_message_id,
            request_id=correlation,
            sender=sender,
            recipient=recipient,
            kind=kind,
        ) from exc
    except OSError as exc:
        raise CommError("The native queue process could not be started") from exc
    receipt = RECEIPT.search(result.stdout)
    if result.returncode != 0 or receipt is None or receipt[2] != recipient:
        raise SendUncertain(
            "Native queue result is uncertain; inspect this request before retrying "
            "(raw CLI output omitted)",
            message_id=logical_message_id,
            request_id=correlation,
            sender=sender,
            recipient=recipient,
            kind=kind,
        )
    return {
        "status": "queued",
        "kind": kind,
        "request_id": correlation,
        "from": sender,
        "to": recipient,
        "message_id": logical_message_id,
        "native_message_id": receipt[1],
    }


def _safe_record(record: dict[str, Any], *, include_text: bool = False):
    frame = record["frame"]
    value = {
        "protocol": frame["protocol"],
        "kind": frame["kind"],
        "from": frame["from"],
        "to": frame["to"],
        "request_id": frame["request_id"],
        "scope_id": frame.get("scope_id"),
        "revision": frame.get("revision"),
        "message_id": frame.get("message_id"),
        "status": frame.get("status"),
        "progress_policy": frame.get("progress_policy"),
        "action": frame.get("action"),
        "sent_at_utc": frame.get("sent_at_utc"),
        "locations": record.get("locations", [record.get("location")]),
        "native_item_ids": record.get(
            "native_item_ids", [record.get("native_item_id")]
        ),
        "turn_ids": record.get("turn_ids", [record.get("turn_id")]),
        "body_sha256": hashlib.sha256(frame["body"].encode("utf-8")).hexdigest(),
        "body_chars": len(frame["body"]),
        "artifacts": frame.get("artifacts", []),
    }
    if include_text:
        value["body"] = frame["body"]
    return value


def check_exchange(
    store: NativeStore,
    actor: str,
    peer: str,
    correlation: str,
    *,
    direction: str = "auto",
    include_text: bool = False,
    verbose: bool = False,
):
    correlation = request_id(correlation)
    peer_raw = store.packets(peer, correlation)
    actor_raw = store.packets(actor, correlation)
    records = _dedupe([*peer_raw, *actor_raw])

    def select(kind: str, source: str, target: str):
        return [
            record
            for record in records
            if record["frame"]["kind"] == kind
            and record["frame"]["from"] == source
            and record["frame"]["to"] == target
        ]

    def has_direction(coordinator: str, worker: str) -> bool:
        return any(
            (
                frame["kind"] in {"request", "notice", "control"}
                and frame["from"] == coordinator
                and frame["to"] == worker
                and (
                    frame["request_id"] == correlation
                    or frame.get("supersedes_request_id") == correlation
                )
            )
            or (
                frame["kind"] in {"progress", "reply"}
                and frame["from"] == worker
                and frame["to"] == coordinator
                and frame["request_id"] == correlation
            )
            for frame in (record["frame"] for record in records)
        )

    if direction not in {"auto", "outbound", "inbound"}:
        raise CommError("direction must be auto, outbound or inbound")
    outbound = has_direction(actor, peer)
    inbound = has_direction(peer, actor)
    if direction == "auto":
        if outbound and inbound:
            raise CommError(
                "Request direction is ambiguous; use --direction outbound or inbound"
            )
        direction = "inbound" if inbound else "outbound"
    coordinator, worker = (actor, peer) if direction == "outbound" else (peer, actor)

    requests = [
        record
        for record in select("request", coordinator, worker)
        if record["frame"]["request_id"] == correlation
    ]
    notices = [
        record
        for record in select("notice", coordinator, worker)
        if record["frame"]["request_id"] == correlation
    ]
    replacements = [
        record
        for record in select("request", coordinator, worker)
        if record["frame"].get("supersedes_request_id") == correlation
    ]
    controls = [
        record
        for record in select("control", coordinator, worker)
        if record["frame"]["request_id"] == correlation
    ]
    progresses = [
        record
        for record in select("progress", worker, coordinator)
        if record["frame"]["request_id"] == correlation
    ]
    replies = [
        record
        for record in select("reply", worker, coordinator)
        if record["frame"]["request_id"] == correlation
    ]
    original = _latest(requests)
    notice = _latest(notices)
    replacement = _latest(replacements)
    close_control = _latest(
        [r for r in controls if r["frame"].get("action") == "close"]
    )
    cancel_control = _latest(
        [r for r in controls if r["frame"].get("action") == "cancel"]
    )
    progress = _latest(progresses)
    ordered_replies = sorted(replies, key=_record_sort_key)
    terminal_candidates = ordered_replies
    late_cancel_ignored = False
    cancel_pending = cancel_control is not None
    if cancel_control is not None:
        pre_cancel = [
            record
            for record in ordered_replies
            if _record_sort_key(record) < _record_sort_key(cancel_control)
        ]
        if pre_cancel:
            terminal_candidates = pre_cancel
            cancel_pending = False
            late_cancel_ignored = True
        else:
            terminal_candidates = [
                record
                for record in ordered_replies
                if record["frame"].get("status") == "cancelled"
            ]
    reply = terminal_candidates[0] if terminal_candidates else None
    duplicate_terminal_count = 0
    conflicting_replies = []
    if reply is not None:
        signature = _frame_payload_key(reply["frame"], ignore_message_id=True)
        for candidate in terminal_candidates[1:]:
            if (
                _frame_payload_key(candidate["frame"], ignore_message_id=True)
                == signature
            ):
                duplicate_terminal_count += 1
            else:
                conflicting_replies.append(candidate)

    delivery_record = original or notice
    delivery_observed = bool(
        delivery_record and "visible_history" in delivery_record.get("locations", [])
    )
    if notice is not None and original is None:
        delivery = "notice_observed" if delivery_observed else "notice_queued"
    else:
        delivery = (
            "request_observed"
            if delivery_observed
            else "queued"
            if delivery_record
            else "not_observed"
        )
    replacement_observed = bool(
        replacement and "visible_history" in replacement.get("locations", [])
    )
    cancel_observed = bool(
        cancel_control and "visible_history" in cancel_control.get("locations", [])
    )

    terminal = False
    effective = True
    stale_replies = len(ordered_replies) - len(terminal_candidates)
    if close_control is not None:
        status = "closed"
        terminal = True
        effective = False
        stale_replies = len(replies)
    elif replacement_observed:
        status = "superseded"
        terminal = True
        effective = False
        stale_replies = len(replies)
    elif replacement is not None:
        status = "supersede_queued"
        effective = False
        stale_replies = len(replies)
    elif conflicting_replies:
        status = "terminal_conflict"
        terminal = True
        effective = False
    elif reply is not None and reply["frame"].get("status") in {
        "cancelled",
        "superseded",
    }:
        status = reply["frame"].get("status", "completed")
        terminal = True
        effective = False
    elif cancel_pending and cancel_observed:
        status = "cancel_observed"
        effective = False
        stale_replies = len(replies)
    elif cancel_pending and cancel_control is not None:
        status = "cancel_queued"
        effective = False
        stale_replies = len(replies)
    elif reply is not None:
        status = reply["frame"].get("status", "completed")
        terminal = True
        effective = True
    elif notice is not None:
        status = delivery
        terminal = delivery_observed
    elif progress is not None:
        status = progress["frame"]["status"]
    else:
        status = delivery

    frame = delivery_record["frame"] if delivery_record else None
    compact = {
        "status": status,
        "terminal": terminal,
        "effective": effective,
        "delivery_status": delivery,
        "request_id": correlation,
        "scope_id": frame.get("scope_id") if frame else None,
        "revision": frame.get("revision") if frame else None,
        "progress_policy": frame.get("progress_policy") if frame else None,
        "sender": actor,
        "peer": peer,
        "direction": direction,
        "coordinator": coordinator,
        "worker": worker,
        "peer_last_turn": store.last_turn(peer),
        "superseded_by": (replacement["frame"]["request_id"] if replacement else None),
        "closed_outcome": (
            close_control["frame"].get("outcome") if close_control else None
        ),
        "late_cancel_ignored": late_cancel_ignored,
        "latest_progress": (
            _safe_record(progress, include_text=include_text) if progress else None
        ),
        "reply": (
            _safe_record(reply, include_text=include_text)
            if reply and (effective or status == "terminal_conflict")
            else None
        ),
        "duplicate_terminal_count": duplicate_terminal_count,
        "terminal_conflict_count": len(conflicting_replies),
        "stale_reply_count": stale_replies,
        "counts": {
            "requests": len(requests),
            "notices": len(notices),
            "replacements": len(replacements),
            "controls": len(controls),
            "progress": len(progresses),
            "replies": len(replies),
        },
    }
    reply_ids = {
        row["native_item_id"]
        for row in [*actor_raw, *peer_raw]
        if row["frame"]["kind"] == "reply"
        and row["location"] == "native_queue"
        and row["frame"]["request_id"] == correlation
        and row["frame"]["from"] == worker
        and row["frame"]["to"] == coordinator
    }
    compact["source_receipts"] = (
        store.source_receipts(worker, coordinator, correlation, reply_ids)
        if replies
        else []
    )
    if verbose:
        compact["records"] = {
            "requests": [_safe_record(r, include_text=False) for r in requests],
            "notices": [_safe_record(r, include_text=False) for r in notices],
            "replacements": [_safe_record(r, include_text=False) for r in replacements],
            "controls": [_safe_record(r, include_text=False) for r in controls],
            "progress": [
                _safe_record(r, include_text=include_text) for r in progresses
            ],
            "replies": [
                _safe_record(
                    r,
                    include_text=include_text
                    and (effective or status == "terminal_conflict"),
                )
                for r in replies
            ],
        }
    return compact


def active_scope_requests(
    store: NativeStore,
    coordinator: str,
    worker: str,
    scope: str,
) -> list[dict[str, Any]]:
    records = _dedupe(
        [
            *store.scope_packets(coordinator, scope),
            *store.scope_packets(worker, scope),
        ]
    )
    correlations = sorted(
        {
            record["frame"]["request_id"]
            for record in records
            if record["frame"]["kind"] == "request"
            and record["frame"]["from"] == coordinator
            and record["frame"]["to"] == worker
            and record["frame"].get("scope_id") == scope
        }
    )
    active = []
    for correlation in correlations:
        state = check_exchange(
            store,
            coordinator,
            worker,
            correlation,
            direction="outbound",
        )
        if not state["terminal"] and (
            state["effective"]
            or state["status"] in {"cancel_queued", "cancel_observed"}
        ):
            active.append(
                {
                    "request_id": correlation,
                    "revision": state["revision"],
                    "status": state["status"],
                }
            )
    return active


def _request_records(
    store: NativeStore,
    coordinator: str,
    worker: str,
    correlation: str,
) -> list[dict[str, Any]]:
    return [
        record
        for record in _dedupe(store.packets(worker, correlation))
        if record["frame"]["kind"] == "request"
        and record["frame"]["request_id"] == correlation
        and record["frame"]["from"] == coordinator
        and record["frame"]["to"] == worker
    ]


def send_or_replay(
    store: NativeStore,
    recipient: str,
    message: str,
    *,
    correlation: str,
    sender: str,
    kind: str,
) -> dict[str, Any]:
    frame = next(frames({"message": message}))
    existing = next(
        (
            record
            for record in _dedupe(store.packets(recipient, correlation))
            if record["frame"].get("message_id") == frame.get("message_id")
        ),
        None,
    )
    if existing is not None:
        if _frame_payload_key(existing["frame"]) != _frame_payload_key(frame):
            raise CommError("message_id was reused with conflicting message content")
        return {
            "status": "already_present",
            "kind": kind,
            "request_id": correlation,
            "from": sender,
            "to": recipient,
            "message_id": frame.get("message_id"),
            "native_message_ids": existing.get("native_item_ids", []),
        }
    return send_native(
        recipient,
        message,
        correlation=correlation,
        sender=sender,
        kind=kind,
    )


def _read_body(args: argparse.Namespace, *, default: str | None = None) -> str:
    try:
        if getattr(args, "file", None):
            body = args.file.expanduser().read_text(encoding="utf-8-sig")
        elif getattr(args, "text", None) is not None:
            body = args.text
        else:
            body = default
    except UnicodeError as exc:
        raise CommError("Message file must be UTF-8") from exc
    if body is None or not body.strip():
        raise CommError("Message body is empty")
    return body


def _target_for_response(
    store: NativeStore,
    sender: str,
    correlation: str,
    explicit: str | None,
    workspace: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    explicit_id = store.resolve(explicit, workspace)["id"] if explicit else None
    request = store.inbound_request(sender, correlation, explicit_id)
    target = store.resolve(request["from"], workspace)
    if explicit_id is not None and target["id"] != explicit_id:
        raise CommError("Explicit reply target does not match the inbound request")
    return target, request


def _add_body(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--message-id",
        help=(
            "Stable logical message ID. Reuse it only when replaying the exact same "
            "message after an uncertain send."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--text", help="Inline UTF-8 message body.")
    group.add_argument(
        "--file", type=Path, help="Read the UTF-8 message body from a file."
    )
    parser.add_argument(
        "--artifact",
        action="append",
        type=Path,
        default=[],
        help="Attach workspace-relative path, SHA-256, and size; repeat as needed.",
    )
    parser.add_argument(
        "--allow-long-body",
        action="store_true",
        help="Override the normal inline body limit.",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    subs.add_parser("identity", help="Show the current thread ID and Codex home.")
    peers = subs.add_parser("peers", help="List candidate Codex sessions.")
    peers.add_argument("--name", help="Filter by exact session name.")
    peers.add_argument("--workspace", help="Filter by workspace path.")
    peers.add_argument(
        "--named-only", action="store_true", help="Hide unnamed sessions."
    )
    peers.add_argument(
        "--exclude-self", action="store_true", help="Hide the current session."
    )

    send = subs.add_parser("send", help="Send a request or no-reply notice.")
    send.add_argument("--to", required=True, help="Exact peer UUID or unique name.")
    send.add_argument("--workspace", help="Limit peer resolution to this workspace.")
    send.add_argument("--request-id", help="Use this correlation ID instead of a UUID.")
    send.add_argument("--scope-id", help="Stable task or Work Package identifier.")
    send.add_argument("--revision", default="1", help="Request revision.")
    send.add_argument("--supersedes", help="Request ID replaced by this revision.")
    send.add_argument(
        "--progress-policy",
        choices=sorted(PROGRESS_POLICIES),
        default="none",
        help="Default quiet mode sends no acknowledgement/progress frames.",
    )
    send.add_argument(
        "--no-reply", action="store_true", help="Send an informational notice."
    )
    _add_body(send, required=True)

    progress = subs.add_parser("progress", help="Send a nonterminal progress update.")
    progress.add_argument("--to", help="Peer UUID or name; inferred when omitted.")
    progress.add_argument(
        "--workspace", help="Limit peer resolution to this workspace."
    )
    progress.add_argument("--request-id", required=True, help="Request being updated.")
    progress.add_argument(
        "--status",
        choices=sorted(PROGRESS_STATUSES),
        default="in_progress",
        help="Current nonterminal state.",
    )
    _add_body(progress, required=False)

    reply = subs.add_parser("reply", help="Send one terminal reply.")
    reply.add_argument("--to", help="Peer UUID or name; inferred when omitted.")
    reply.add_argument("--workspace", help="Limit peer resolution to this workspace.")
    reply.add_argument("--request-id", required=True, help="Request being completed.")
    reply.add_argument(
        "--status",
        choices=sorted(TERMINAL_STATUSES),
        default="completed",
        help="Terminal result.",
    )
    _add_body(reply, required=False)

    for command in ("cancel", "close"):
        command_help = (
            "Ask the peer to stop work cooperatively."
            if command == "cancel"
            else "Record the outcome and make later replies stale."
        )
        control = subs.add_parser(command, help=command_help)
        control.add_argument(
            "--to", required=True, help="Exact peer UUID or unique name."
        )
        control.add_argument(
            "--workspace", help="Limit peer resolution to this workspace."
        )
        control.add_argument("--request-id", required=True, help="Target request ID.")
        if command == "cancel":
            control.add_argument(
                "--replacement-request-id",
                help="Optional request ID that replaces the cancelled work.",
            )
        else:
            control.add_argument(
                "--outcome",
                choices=sorted(CLOSE_OUTCOMES),
                default="accepted",
                help="Coordinator's recorded outcome.",
            )
        _add_body(control, required=False)

    check = subs.add_parser("check", help="Read lifecycle state for one request.")
    check.add_argument(
        "--peer", required=True, help="Other endpoint UUID or unique name."
    )
    check.add_argument("--workspace", help="Limit peer resolution to this workspace.")
    check.add_argument("--request-id", required=True, help="Request correlation ID.")
    check.add_argument(
        "--wait-seconds", type=float, default=0, help="Wait briefly for a state change."
    )
    check.add_argument(
        "--direction",
        choices=("auto", "outbound", "inbound"),
        default="auto",
        help="Request direction from the current session.",
    )
    check.add_argument(
        "--include-text",
        action="store_true",
        help="Include only the current effective reply body.",
    )
    check.add_argument(
        "--verbose",
        action="store_true",
        help="Include raw matched lifecycle records for troubleshooting.",
    )
    args = parser.parse_args(argv)
    try:
        store = NativeStore(codex_home())
        if args.command == "identity":
            result = {"thread_id": current_thread(), "codex_home": str(store.root)}
        elif args.command == "peers":
            current = current_thread() if args.exclude_self else None
            rows = store.peers(
                args.name,
                args.workspace,
                named_only=args.named_only,
                exclude=current,
            )
            result = {"count": len(rows), "peers": rows}
        elif args.command == "send":
            sender = current_thread()
            target = store.resolve(args.to, args.workspace)
            if sender == target["id"]:
                raise CommError(
                    "Refusing an accidental self-message; select another session"
                )
            correlation = request_id(args.request_id or uuid.uuid4().hex)
            body = _read_body(args)
            kind = "notice" if args.no_reply else "request"
            _body_limit(kind, body, args.allow_long_body)
            artifacts = artifact_metadata(args.artifact)
            supersedes = request_id(args.supersedes) if args.supersedes else None
            if supersedes == correlation:
                raise CommError("A request cannot supersede itself")
            scope = token("scope_id", args.scope_id or correlation)
            message = frame_message(
                kind,
                sender,
                target["id"],
                correlation,
                body,
                scope_id=scope,
                revision=args.revision,
                supersedes=supersedes,
                progress_policy=(args.progress_policy if kind == "request" else None),
                artifacts=artifacts,
                message_id=args.message_id,
            )
            frame = next(frames({"message": message}))
            existing = (
                _request_records(store, sender, target["id"], correlation)
                if kind == "request"
                else []
            )
            if existing:
                if not any(
                    record["frame"].get("message_id") == frame.get("message_id")
                    for record in existing
                ):
                    raise CommError(
                        "request_id already exists with another message_id; inspect "
                        "the existing request or create a new revision"
                    )
                result = send_or_replay(
                    store,
                    target["id"],
                    message,
                    correlation=correlation,
                    sender=sender,
                    kind=kind,
                )
            else:
                if kind == "request":
                    active = active_scope_requests(store, sender, target["id"], scope)
                    active_ids = {item["request_id"] for item in active}
                    if active_ids and active_ids != {supersedes}:
                        raise CommError(
                            "An active request already owns this peer and scope; "
                            "close it or explicitly supersede its request_id"
                        )
                    if supersedes is not None:
                        replaced = _request_records(
                            store, sender, target["id"], supersedes
                        )
                        if not replaced:
                            raise CommError(
                                "The superseded request was not found for this peer"
                            )
                        if any(
                            record["frame"].get("scope_id") != scope
                            for record in replaced
                        ):
                            raise CommError(
                                "A replacement must keep the superseded request scope"
                            )
                result = send_or_replay(
                    store,
                    target["id"],
                    message,
                    correlation=correlation,
                    sender=sender,
                    kind=kind,
                )
            result.update(
                {
                    "scope_id": scope,
                    "revision": args.revision,
                    "progress_policy": (
                        args.progress_policy if kind == "request" else None
                    ),
                    "supersedes_request_id": supersedes,
                    "recipient": target,
                    "body_chars": len(body),
                    "artifacts": artifacts,
                }
            )
        elif args.command in {"progress", "reply"}:
            sender = current_thread()
            correlation = request_id(args.request_id)
            target, request = _target_for_response(
                store, sender, correlation, args.to, args.workspace
            )
            if (
                args.command == "progress"
                and request.get("progress_policy", "on-change") == "none"
            ):
                raise CommError(
                    "This request uses progress_policy=none; send one terminal reply "
                    "instead"
                )
            default = (
                "Progress acknowledged."
                if args.command == "progress"
                else f"Terminal status: {args.status}."
            )
            body = _read_body(args, default=default)
            _body_limit(args.command, body, args.allow_long_body)
            artifacts = artifact_metadata(args.artifact)
            message = frame_message(
                args.command,
                sender,
                target["id"],
                correlation,
                body,
                status=args.status,
                scope_id=request.get("scope_id"),
                revision=request.get("revision"),
                artifacts=artifacts,
                message_id=args.message_id,
            )
            result = send_or_replay(
                store,
                target["id"],
                message,
                correlation=correlation,
                sender=sender,
                kind=args.command,
            )
            result.update(
                {
                    "status_value": args.status,
                    "scope_id": request.get("scope_id"),
                    "revision": request.get("revision"),
                    "progress_policy": request.get("progress_policy"),
                    "recipient": target,
                    "body_chars": len(body),
                    "artifacts": artifacts,
                }
            )
        elif args.command in {"cancel", "close"}:
            sender = current_thread()
            target = store.resolve(args.to, args.workspace)
            correlation = request_id(args.request_id)
            body = _read_body(
                args,
                default=(
                    "Cancel this request when observed."
                    if args.command == "cancel"
                    else "Coordinator closed this request."
                ),
            )
            _body_limit("control", body, args.allow_long_body)
            artifacts = artifact_metadata(args.artifact)
            try:
                request = [
                    record["frame"]
                    for record in _dedupe(store.packets(target["id"], correlation))
                    if record["frame"]["kind"] == "request"
                    and record["frame"]["request_id"] == correlation
                    and record["frame"]["from"] == sender
                    and record["frame"]["to"] == target["id"]
                ][-1]
            except IndexError:
                request = {
                    "scope_id": correlation,
                    "revision": "1",
                }
            replacement = (
                request_id(args.replacement_request_id)
                if args.command == "cancel" and args.replacement_request_id
                else None
            )
            message = frame_message(
                "control",
                sender,
                target["id"],
                correlation,
                body,
                scope_id=request.get("scope_id"),
                revision=request.get("revision"),
                action=args.command,
                replacement=replacement,
                outcome=args.outcome if args.command == "close" else None,
                artifacts=artifacts,
                message_id=args.message_id,
            )
            result = send_or_replay(
                store,
                target["id"],
                message,
                correlation=correlation,
                sender=sender,
                kind="control",
            )
            result.update(
                {
                    "action": args.command,
                    "replacement_request_id": replacement,
                    "outcome": args.outcome if args.command == "close" else None,
                    "recipient": target,
                }
            )
        else:
            if (
                not isinstance(args.wait_seconds, (int, float))
                or not 0 <= args.wait_seconds <= 50
            ):
                raise CommError("wait-seconds must be finite and between 0 and 50")
            sender = current_thread()
            peer = store.resolve(args.peer, args.workspace)["id"]
            correlation = request_id(args.request_id)
            deadline = time.monotonic() + args.wait_seconds
            while True:
                result = check_exchange(
                    store,
                    sender,
                    peer,
                    correlation,
                    direction=args.direction,
                    include_text=args.include_text,
                    verbose=args.verbose,
                )
                if result["terminal"] or time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CommError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        status = "send_uncertain" if isinstance(exc, SendUncertain) else "error"
        failure = {
            "status": status,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        if isinstance(exc, SendUncertain):
            failure.update(
                {
                    "message_id": exc.message_id,
                    "request_id": exc.request_id,
                    "from": exc.sender,
                    "to": exc.recipient,
                    "kind": exc.kind,
                }
            )
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
