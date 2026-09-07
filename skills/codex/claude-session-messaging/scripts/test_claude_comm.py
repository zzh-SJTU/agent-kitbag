"""Offline tests for the local Claude Code peer bridge."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import claude_comm as comm

SESSION = "3edbb5c4-6ac9-4f8f-ac34-ed240a3968c8"
PIPE = r"\\.\pipe\LOCAL\cc-msg-4e4e95ebec61b0a012b1315bbd28017a"
PEER_TOKEN = "a" * 32
REQUEST = "cross-runtime-test-01"


class ClaudeCommunicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-peer-test-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        (self.root / "sessions").mkdir()
        self.project = self.root / "projects" / "F--meeting-assistant"
        self.project.mkdir(parents=True)
        self.log = self.project / f"{SESSION}.jsonl"
        self.log.write_bytes(b'{"type":"baseline"}\n')
        self.peer = {
            "session_id": SESSION,
            "name": "meeting-assistant-e3",
            "cwd": r"F:\meeting_assistant",
            "status": "idle",
            "kind": "interactive",
            "pid": 14964,
        }
        record = {
            "pid": 14964,
            "sessionId": SESSION,
            "cwd": self.peer["cwd"],
            "messagingSocketPath": PIPE,
            "peerProtocol": 1,
            "peerFeatures": ["notify_idle", "artifact_yield"],
            "procStartFt": "12345",
            "pidDomain": "windows-domain",
        }
        (self.root / "sessions" / "14964.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        key = {
            "peerToken": PEER_TOKEN,
            "procStartFt": "12345",
            "pidDomain": "windows-domain",
        }
        (self.root / "sessions" / ("14964." + "b" * 64 + ".key")).write_text(
            json.dumps(key), encoding="utf-8"
        )
        self.home = patch.object(comm, "claude_home", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)

    def test_peer_discovery_is_safe_and_exact(self):
        raw = [
            {
                "sessionId": SESSION,
                "name": "meeting-assistant-e3",
                "cwd": self.peer["cwd"],
                "status": "idle",
                "kind": "interactive",
                "pid": 14964,
                "secret": "not returned",
            }
        ]
        with (
            patch.object(comm.shutil, "which", return_value="claude.exe"),
            patch.object(comm, "_run_json", return_value=raw),
        ):
            peers = comm.peers(self.peer["cwd"])
        self.assertEqual(peers, [self.peer])
        self.assertNotIn("secret", peers[0])
        with patch.object(comm, "peers", return_value=peers):
            self.assertEqual(
                comm.resolve_peer("meeting-assistant-e3", self.peer["cwd"]), self.peer
            )
            self.assertEqual(comm.resolve_peer(SESSION), self.peer)

    def test_registry_and_auth_identity_are_verified_without_exposure(self):
        record = comm._registry_record(self.peer)
        self.assertEqual(record["messagingSocketPath"], PIPE)
        self.assertEqual(comm._peer_token(record), PEER_TOKEN)
        record["procStartFt"] = "changed"
        with self.assertRaisesRegex(comm.CommError, "identity changed") as cm:
            comm._peer_token(record)
        self.assertNotIn(PEER_TOKEN, str(cm.exception))

    def test_missing_optional_registry_start_time_keeps_domain_verification(self):
        record = comm._registry_record(self.peer)
        record.pop("procStartFt")
        self.assertEqual(comm._peer_token(record), PEER_TOKEN)
        record["pidDomain"] = "different-domain"
        with self.assertRaisesRegex(comm.CommError, "identity changed"):
            comm._peer_token(record)

    def test_registry_rejects_a_peer_that_changed_workspace(self):
        record_path = self.root / "sessions" / "14964.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["cwd"] = r"F:\other-project"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(comm.CommError, "workspace changed"):
            comm._registry_record(self.peer)

    def test_probe_authenticates_without_sending_a_user_frame(self):
        with patch.object(comm, "_node_send") as send:
            result = comm.probe(self.peer)
        self.assertEqual(result["status"], "reachable")
        self.assertEqual(result["protocol"], 1)
        self.assertEqual(send.call_args.args[2], [])
        self.assertNotIn(PEER_TOKEN, json.dumps(result))

    def test_request_frame_is_scoped_and_contains_no_peer_token(self):
        text = comm.request_text(self.peer, REQUEST, "Return a synthetic result.")
        self.assertTrue(text.startswith(comm.REQUEST_PREFIX))
        first = json.loads(text.splitlines()[0][len(comm.REQUEST_PREFIX) :])
        self.assertEqual(first["request_id"], REQUEST)
        self.assertEqual(first["to_session_id"], SESSION)
        self.assertEqual(first["body"], "Return a synthetic result.")
        self.assertIn(comm.REPLY_PREFIX, text)
        self.assertEqual(text.count("Return a synthetic result."), 1)
        self.assertNotIn(PEER_TOKEN, text)

    def test_send_records_cursor_and_uses_only_the_user_frame(self):
        baseline = self.log.stat().st_size
        with patch.object(comm, "_node_send") as send:
            result = comm.send_request(self.peer, REQUEST, "Synthetic request.")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["cursor"], baseline)
        frames = send.call_args.args[2]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "user")
        self.assertEqual(frames[0]["msg_id"], REQUEST)
        self.assertTrue(frames[0]["from"].startswith("codex"))
        self.assertIn(REQUEST, frames[0]["message"]["content"])
        self.assertNotIn(PEER_TOKEN, json.dumps(frames))

    def test_send_uncertain_preserves_cursor_for_wait_recovery(self):
        baseline = self.log.stat().st_size
        with (
            patch.object(
                comm,
                "_node_send",
                side_effect=comm.SendUncertain("uncertain"),
            ),
            self.assertRaises(comm.SendUncertain) as cm,
        ):
            comm.send_request(self.peer, REQUEST, "Synthetic request.")
        self.assertEqual(cm.exception.cursor, baseline)
        self.assertEqual(cm.exception.request_id, REQUEST)
        self.assertEqual(cm.exception.session_id, SESSION)
        self.assertEqual(cm.exception.log, str(self.log))

    def test_reply_reader_tolerates_a_short_preamble(self):
        expected = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "valid response",
        }
        embedded = "Commentary before " + comm.REPLY_PREFIX + json.dumps(expected)
        self.assertEqual(comm._reply_from_text(embedded, REQUEST), expected)

    def test_reply_reader_ignores_the_prompt_placeholder(self):
        placeholder = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "<concise response>",
        }
        self.assertIsNone(
            comm._reply_from_text(
                "Quoted contract " + comm.REPLY_PREFIX + json.dumps(placeholder),
                REQUEST,
            )
        )

    def test_reply_reader_rejects_conflicts_inside_one_text_block(self):
        first = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "first",
        }
        second = {**first, "body": "second"}
        text = (
            comm.REPLY_PREFIX
            + json.dumps(first)
            + "\n"
            + comm.REPLY_PREFIX
            + json.dumps(second)
        )
        with self.assertRaisesRegex(comm.CommError, "conflicting"):
            comm._reply_from_text(text, REQUEST)

    def test_wait_reads_only_new_assistant_text_and_returns_structured_reply(self):
        cursor = self.log.stat().st_size
        reply = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "Synthetic reply.",
        }
        line = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": comm.REPLY_PREFIX + json.dumps(reply),
                    }
                ],
            },
        }
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\n")
        self.assertEqual(
            comm.wait_reply(self.log, REQUEST, cursor, 0),
            reply,
        )

    def test_wait_rejects_conflicting_terminal_replies(self):
        cursor = self.log.stat().st_size
        for body in ("first", "second"):
            reply = {
                "protocol": comm.PROTOCOL,
                "request_id": REQUEST,
                "status": "completed",
                "body": body,
            }
            line = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": comm.REPLY_PREFIX + json.dumps(reply),
                        }
                    ],
                },
            }
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(line) + "\n")
        with self.assertRaisesRegex(comm.CommError, "conflicting"):
            comm.wait_reply(self.log, REQUEST, cursor, 0)

    def test_wait_timeout_is_a_recoverable_pending_state(self):
        cursor = self.log.stat().st_size
        with self.assertRaisesRegex(comm.ReplyPending, "not observed"):
            comm.wait_reply(self.log, REQUEST, cursor, 0)

    def test_cli_request_wait_preserves_cursor_when_reply_is_pending(self):
        queued = {
            "status": "queued",
            "request_id": REQUEST,
            "peer": self.peer,
            "cursor": 42,
            "log": str(self.log),
        }
        with (
            patch.object(comm, "resolve_peer", return_value=self.peer),
            patch.object(comm, "send_request", return_value=queued),
            patch.object(
                comm,
                "wait_reply",
                side_effect=comm.ReplyPending("reply pending"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "request",
                    "--to",
                    SESSION,
                    "--request-id",
                    REQUEST,
                    "--wait-seconds",
                    "1",
                    "--text",
                    "synthetic",
                ]
            )
        self.assertEqual(code, 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["cursor"], 42)
        self.assertEqual(result["request_id"], REQUEST)

    def test_reply_body_is_hidden_by_default(self):
        reply = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "private synthetic body",
        }
        safe = comm._safe_reply(reply, False)
        self.assertNotIn("body", safe)
        self.assertEqual(safe["body_chars"], len(reply["body"]))
        self.assertIn("body", comm._safe_reply(reply, True))

    def test_invalid_pipe_and_oversized_body_fail_before_send(self):
        record_path = self.root / "sessions" / "14964.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["messagingSocketPath"] = r"\\server\share\pipe"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(comm.CommError, "supported local inbox"):
            comm._registry_record(self.peer)
        with self.assertRaisesRegex(comm.CommError, "exceeds"):
            comm.request_text(self.peer, REQUEST, "x" * (comm.BODY_LIMIT + 1))

    def test_cli_error_never_prints_peer_token(self):
        with (
            patch.object(comm, "resolve_peer", return_value=self.peer),
            patch.object(
                comm,
                "probe",
                side_effect=comm.CommError("Claude peer authentication failed"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(["probe", "--to", SESSION])
        self.assertEqual(code, 2)
        self.assertNotIn(PEER_TOKEN, output.getvalue())

    def test_cli_send_uncertain_returns_wait_recovery_fields(self):
        with (
            patch.object(comm, "resolve_peer", return_value=self.peer),
            patch.object(
                comm,
                "_node_send",
                side_effect=comm.SendUncertain("uncertain"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "request",
                    "--to",
                    SESSION,
                    "--request-id",
                    REQUEST,
                    "--text",
                    "synthetic",
                ]
            )
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "send_uncertain")
        self.assertEqual(result["request_id"], REQUEST)
        self.assertEqual(result["to_session_id"], SESSION)
        self.assertEqual(result["cursor"], self.log.stat().st_size)
        self.assertIn("do not resend", result["recovery"])


if __name__ == "__main__":
    unittest.main()
