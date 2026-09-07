"""Offline tests for the Claude -> Codex session bridge.

Fixtures build disposable Codex-store-shaped sqlite files and mock `codex queue`;
no real message is sent and no live store is touched.
"""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import codex_comm as comm

SESSION = "3edbb5c4-6ac9-4f8f-ac34-ed240a3968c8"
NEWER = "99999999-8888-7777-6666-555555555555"
OTHER = "11111111-2222-3333-4444-555555555555"
WORKSPACE = r"F:\meeting_assistant"
REQUEST = "claude-codex-test-01"


def _agent_message(text: str) -> str:
    return json.dumps({"type": "agentMessage", "text": text, "phase": "final_answer"})


def _frame(request_id: str, status: str, body: str) -> str:
    return comm.REPLY_PREFIX + json.dumps(
        {
            "protocol": comm.PROTOCOL,
            "request_id": request_id,
            "status": status,
            "body": body,
        }
    )


def _reply_text(request_id: str, status: str, body: str) -> str:
    # A realistic reply: a short preamble precedes the frame (reader-lenient).
    return "some preamble\n" + _frame(request_id, status, body)


class CodexBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-bridge-test-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self._build_state()
        self._build_history()
        self.home = patch.object(comm, "codex_home", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)

    def _build_state(self):
        con = sqlite3.connect(self.root / "state_1.sqlite")
        con.execute(
            "CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, archived INTEGER, "
            "recency_at_ms INTEGER)"
        )
        con.executemany(
            "INSERT INTO threads VALUES (?,?,?,?,?)",
            [
                (SESSION, "meeting-worker", WORKSPACE, 0, 2000),
                (NEWER, "fresh-worker", WORKSPACE, 0, 9000),
                (OTHER, "elsewhere", r"C:\other", 0, 5000),
                ("dead", "archived-one", WORKSPACE, 1, 8000),
            ],
        )
        con.commit()
        con.close()

    def _build_history(self):
        con = sqlite3.connect(self.root / "thread_history_1.sqlite")
        con.execute(
            "CREATE TABLE thread_items (thread_id TEXT, item_type TEXT, "
            "item_json TEXT, rollout_ordinal INTEGER)"
        )
        con.executemany(
            "INSERT INTO thread_items VALUES (?,?,?,?)",
            [
                (SESSION, "userMessage", '{"type":"userMessage","text":"hi"}', 1),
                (SESSION, "agentMessage", _agent_message("old unrelated answer"), 2),
            ],
        )
        con.commit()
        con.close()

    def _append(self, thread_id, item_type, text, ordinal):
        con = sqlite3.connect(self.root / "thread_history_1.sqlite")
        con.execute(
            "INSERT INTO thread_items VALUES (?,?,?,?)",
            (thread_id, item_type, text, ordinal),
        )
        con.commit()
        con.close()

    # --- discovery -----------------------------------------------------------
    def test_peers_are_active_workspace_scoped_and_recency_ordered(self):
        rows = comm.peers(WORKSPACE)
        # active + in-workspace only; archived and other-workspace excluded
        self.assertEqual({r["session_id"] for r in rows}, {NEWER, SESSION})
        # most-recently-active first
        self.assertEqual([r["session_id"] for r in rows], [NEWER, SESSION])
        self.assertEqual(rows[0]["recency_ms"], 9000)
        self.assertEqual(comm.resolve_peer(SESSION)["session_id"], SESSION)
        self.assertEqual(
            comm.resolve_peer("meeting-worker", WORKSPACE)["session_id"], SESSION
        )

    def test_peers_discovery_works_without_recency_column(self):
        # Older stores may lack any recency column; discovery must still work.
        con = sqlite3.connect(self.root / "state_1.sqlite")
        con.execute("CREATE TABLE t (id TEXT, name TEXT, cwd TEXT, archived INTEGER)")
        con.execute("DROP TABLE threads")
        con.execute("ALTER TABLE t RENAME TO threads")
        con.execute(
            "INSERT INTO threads VALUES (?,?,?,?)", (SESSION, "solo", WORKSPACE, 0)
        )
        con.commit()
        con.close()
        rows = comm.peers(WORKSPACE)
        self.assertEqual(
            rows,
            [
                {
                    "session_id": SESSION,
                    "name": "solo",
                    "cwd": WORKSPACE,
                    "recency_ms": None,
                }
            ],
        )

    def test_resolve_rejects_ambiguous_or_missing(self):
        with self.assertRaisesRegex(comm.CommError, "matched 0"):
            comm.resolve_peer("no-such-session")

    # --- request framing -----------------------------------------------------
    def test_request_text_is_scoped_and_carries_reply_contract(self):
        peer = {"session_id": SESSION, "name": "meeting-worker", "cwd": WORKSPACE}
        text = comm.request_text(peer, REQUEST, "Do a scoped thing.")
        self.assertTrue(text.startswith(comm.REQUEST_PREFIX))
        first = json.loads(text.splitlines()[0][len(comm.REQUEST_PREFIX) :])
        self.assertEqual(first["protocol"], comm.PROTOCOL)
        self.assertEqual(first["request_id"], REQUEST)
        self.assertEqual(first["from"], "claude")
        self.assertEqual(first["to_session_id"], SESSION)
        self.assertIn(comm.REPLY_PREFIX, text)

    def test_request_body_appears_exactly_once(self):
        peer = {"session_id": SESSION, "name": "meeting-worker", "cwd": WORKSPACE}
        text = comm.request_text(peer, REQUEST, "UNIQUE-BODY-MARKER-7")
        self.assertEqual(text.count("UNIQUE-BODY-MARKER-7"), 1)
        # the envelope frame carries routing/identity only, not the body
        first = json.loads(text.splitlines()[0][len(comm.REQUEST_PREFIX) :])
        self.assertNotIn("body", first)

    def test_oversized_body_fails_before_send(self):
        peer = {"session_id": SESSION, "name": "w", "cwd": WORKSPACE}
        with self.assertRaisesRegex(comm.CommError, "exceeds"):
            comm.request_text(peer, REQUEST, "x" * (comm.BODY_LIMIT + 1))

    # --- send ----------------------------------------------------------------
    def test_send_records_cursor_and_queues_once(self):
        peer = comm.resolve_peer(SESSION)
        with patch.object(
            comm,
            "_queue",
            return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ) as queue:
            result = comm.send_request(peer, REQUEST, "Synthetic request.")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["cursor"], 2)  # max ordinal before send
        queue.assert_called_once()
        queued_text = queue.call_args.args[1]
        self.assertIn(REQUEST, queued_text)

    # --- readback ------------------------------------------------------------
    def test_wait_returns_only_matching_reply_after_cursor(self):
        self._append(
            SESSION,
            "agentMessage",
            _agent_message(_reply_text(REQUEST, "completed", "done")),
            5,
        )
        reply = comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)
        self.assertEqual(reply["status"], "completed")
        self.assertEqual(reply["body"], "done")

    def test_wait_ignores_reply_before_cursor(self):
        self._append(
            SESSION,
            "agentMessage",
            _agent_message(_reply_text(REQUEST, "completed", "stale")),
            2,
        )
        with self.assertRaises(comm.ReplyPending):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_ignores_wrong_request_id(self):
        self._append(
            SESSION,
            "agentMessage",
            _agent_message(_reply_text("other-id", "completed", "x")),
            5,
        )
        with self.assertRaises(comm.ReplyPending):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_rejects_conflicting_terminal_replies(self):
        self._append(
            SESSION,
            "agentMessage",
            _agent_message(_reply_text(REQUEST, "completed", "a")),
            5,
        )
        self._append(
            SESSION,
            "agentMessage",
            _agent_message(_reply_text(REQUEST, "failed", "b")),
            6,
        )
        with self.assertRaisesRegex(comm.CommError, "conflicting"):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_timeout_is_recoverable_pending(self):
        with self.assertRaisesRegex(comm.ReplyPending, "not observed"):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_ignores_placeholder_reply_body(self):
        # Codex quoting the request contract must not be read as a real reply.
        placeholder = _reply_text(REQUEST, "completed", "<concise response>")
        self._append(SESSION, "agentMessage", _agent_message(placeholder), 5)
        with self.assertRaises(comm.ReplyPending):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_rejects_conflict_within_one_message(self):
        # Two conflicting valid frames in ONE agentMessage text must fail closed.
        frame_a = _frame(REQUEST, "completed", "a")
        frame_b = _frame(REQUEST, "failed", "b")
        both = _agent_message(frame_a + "\n" + frame_b)
        self._append(SESSION, "agentMessage", both, 5)
        with self.assertRaisesRegex(comm.CommError, "conflicting"):
            comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)

    def test_wait_accepts_identical_duplicate_frames_in_one_message(self):
        frame = _frame(REQUEST, "completed", "same")
        self._append(SESSION, "agentMessage", _agent_message(frame + "\n" + frame), 5)
        reply = comm.wait_reply(SESSION, REQUEST, cursor=2, wait_seconds=0)
        self.assertEqual(reply["body"], "same")

    # --- reply presentation --------------------------------------------------
    def test_reply_body_hidden_unless_requested(self):
        reply = {
            "protocol": comm.PROTOCOL,
            "request_id": REQUEST,
            "status": "completed",
            "body": "private",
        }
        self.assertNotIn("body", comm._safe_reply(reply, False))
        self.assertEqual(comm._safe_reply(reply, False)["body_chars"], len("private"))
        self.assertEqual(comm._safe_reply(reply, True)["body"], "private")

    # --- CLI -----------------------------------------------------------------
    def test_cli_send_wait_reports_waiting_and_preserves_cursor(self):
        with (
            patch.object(
                comm, "_queue", return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    SESSION,
                    "--request-id",
                    REQUEST,
                    "--wait-seconds",
                    "0.01",
                    "--text",
                    "synthetic",
                ]
            )
        self.assertEqual(code, 3)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["cursor"], 2)
        self.assertEqual(payload["request_id"], REQUEST)

    def test_cli_send_uncertain_carries_cursor_for_recovery(self):
        with (
            patch.object(comm, "_queue", side_effect=comm.SendUncertain("uncertain")),
            redirect_stdout(io.StringIO()) as out,
        ):
            code = comm.main(
                ["send", "--to", SESSION, "--request-id", REQUEST, "--text", "x"]
            )
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "send_uncertain")
        # the pre-send cursor (max ordinal in fixture history = 2) is returned so
        # the caller can re-check for a possibly-delivered reply instead of resending
        self.assertEqual(payload["cursor"], 2)
        self.assertEqual(payload["request_id"], REQUEST)
        self.assertEqual(payload["to_session_id"], SESSION)
        self.assertIn("recovery", payload)


if __name__ == "__main__":
    unittest.main()
