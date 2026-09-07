"""Disposable protocol/lifecycle tests; never access real Codex stores."""

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_comm as comm

MAIN = "00000000-0000-4000-8000-000000000001"
PEER = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
MESSAGE = "00000000-0000-4000-8000-000000000004"
REQUEST = "roundtrip-unique-01"
REPLACEMENT = "roundtrip-unique-02"
SCOPE = "WP-TEST"


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-agent-msg-test-")
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = comm.NativeStore(self.root)
        self.ordinal = 0
        self.execute(
            "state",
            "CREATE TABLE threads (id TEXT,name TEXT,cwd TEXT,archived INTEGER)",
        )
        self.execute(
            "state",
            "INSERT INTO threads VALUES (?,?,?,0)",
            (MAIN, "Coordinator", r"\\?\D:\shared"),
        )
        self.execute(
            "state",
            "INSERT INTO threads VALUES (?,?,?,0)",
            (PEER, "Reviewer", r"\\?\D:\shared"),
        )
        self.execute(
            "state",
            "INSERT INTO threads VALUES (?,?,?,0)",
            (OTHER, None, r"D:\other"),
        )
        self.execute(
            "queue",
            "CREATE TABLE queued_items (id TEXT,thread_id TEXT,payload_json TEXT)",
        )
        self.execute(
            "thread_history",
            "CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,"
            "item_type TEXT,item_json TEXT,rollout_ordinal INTEGER)",
        )
        self.execute(
            "thread_history",
            "CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,"
            "status TEXT,rollout_ordinal INTEGER)",
        )
        self.execute(
            "thread_history",
            "INSERT INTO thread_turns VALUES (?,?,?,1)",
            (PEER, "peer-turn", "completed"),
        )

    def database(self, stem):
        return self.root / (stem + "_1.sqlite")

    def execute(self, stem, sql, params=()):
        con = sqlite3.connect(self.database(stem))
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def queue(self, recipient, text, item_id=MESSAGE):
        payload = {"UserInput": {"input": [{"type": "text", "text": text}]}}
        self.execute(
            "queue",
            "INSERT INTO queued_items VALUES (?,?,?)",
            (item_id, recipient, json.dumps(payload)),
        )

    def history(self, recipient, text, item_type="userMessage"):
        self.ordinal += 1
        item = {"type": item_type, "content": [{"type": "text", "text": text}]}
        self.execute(
            "thread_history",
            "INSERT INTO thread_items VALUES (?,?,?,?,?,?)",
            (
                recipient,
                "peer-turn",
                "history-" + str(self.ordinal),
                item_type,
                json.dumps(item),
                self.ordinal,
            ),
        )

    def request(self, request_id=REQUEST, **kwargs):
        return comm.frame_message(
            "request",
            MAIN,
            PEER,
            request_id,
            kwargs.pop("body", "Do scoped work."),
            scope_id=kwargs.pop("scope_id", SCOPE),
            revision=kwargs.pop("revision", "1"),
            **kwargs,
        )

    def progress(self, status="in_progress", body="Working."):
        return comm.frame_message(
            "progress",
            PEER,
            MAIN,
            REQUEST,
            body,
            status=status,
            scope_id=SCOPE,
            revision="1",
        )

    def reply(self, status="completed", body="Done."):
        return comm.frame_message(
            "reply",
            PEER,
            MAIN,
            REQUEST,
            body,
            status=status,
            scope_id=SCOPE,
            revision="1",
        )

    def control(self, action, **kwargs):
        return comm.frame_message(
            "control",
            MAIN,
            PEER,
            REQUEST,
            kwargs.pop("body", action),
            scope_id=SCOPE,
            revision="1",
            action=action,
            **kwargs,
        )

    def check(self, **kwargs):
        return comm.check_exchange(self.store, MAIN, PEER, REQUEST, **kwargs)

    def test_name_resolution_is_exact_and_workspace_disambiguates(self):
        self.execute(
            "state",
            "UPDATE threads SET name='Reviewer' WHERE id=?",
            (OTHER,),
        )
        with self.assertRaisesRegex(comm.CommError, "matched 2"):
            self.store.resolve("Reviewer")
        self.assertEqual(self.store.resolve("Reviewer", "d:/shared")["id"], PEER)
        with self.assertRaisesRegex(comm.CommError, "matched 0"):
            self.store.resolve("Review")

    def test_peer_filters_remove_unnamed_and_self(self):
        rows = self.store.peers(named_only=True, exclude=MAIN)
        self.assertEqual(
            [(row["id"], row["name"]) for row in rows], [(PEER, "Reviewer")]
        )

    def test_uuid_takes_precedence_over_uuid_shaped_name(self):
        self.execute("state", "UPDATE threads SET name=? WHERE id=?", (PEER, OTHER))
        self.assertEqual(self.store.resolve(PEER)["id"], PEER)

    def test_archived_recipient_is_not_reactivated(self):
        self.execute("state", "UPDATE threads SET archived=1 WHERE id=?", (PEER,))
        with self.assertRaisesRegex(comm.CommError, "matched 0"):
            self.store.resolve(PEER)

    def test_native_reader_is_query_only_and_schema_checked(self):
        with (
            self.store.open("queue", {"queued_items": {"id"}}) as con,
            self.assertRaises(sqlite3.OperationalError),
        ):
            con.execute("DELETE FROM queued_items")
        with (
            self.assertRaisesRegex(comm.CommError, "Unsupported"),
            self.store.open("queue", {"queued_items": {"missing"}}),
        ):
            self.fail("unsupported schema opened")

    def test_missing_metadata_does_not_create_database(self):
        with (
            self.assertRaisesRegex(comm.CommError, "unavailable"),
            self.store.open("missing", {}),
        ):
            pass
        self.assertFalse(list(self.root.glob("missing*")))

    def test_identity_requires_consistent_environment(self):
        with patch.dict(
            os.environ, {"CODEX_THREAD_ID": MAIN, "CODEX_SESSION_ID": MAIN}
        ):
            self.assertEqual(comm.current_thread(), MAIN)
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": MAIN, "CODEX_SESSION_ID": PEER}),
            self.assertRaisesRegex(comm.CommError, "disagree"),
        ):
            comm.current_thread()

    def test_v1_frames_remain_readable_and_terminal(self):
        request = comm.frame_message(
            "request", MAIN, PEER, REQUEST, "legacy", protocol=comm.PROTOCOL_V1
        )
        reply = comm.frame_message(
            "reply", PEER, MAIN, REQUEST, "legacy reply", protocol=comm.PROTOCOL_V1
        )
        self.history(PEER, request)
        self.queue(MAIN, reply)
        result = self.check()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["scope_id"], REQUEST)
        self.assertEqual(result["revision"], "legacy")
        request_frame = next(comm.frames({"text": request}))
        self.assertEqual(request_frame["progress_policy"], "on-change")

    def test_v2_request_has_scope_revision_supersede_and_artifacts(self):
        artifact = [{"path": "docs/handoff.md", "sha256": "a" * 64, "bytes": 12}]
        message = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REPLACEMENT,
            "revision two",
            scope_id=SCOPE,
            revision="2",
            supersedes=REQUEST,
            artifacts=artifact,
        )
        frame = next(comm.frames({"text": message}))
        self.assertEqual(frame["protocol"], comm.PROTOCOL)
        self.assertEqual(frame["scope_id"], SCOPE)
        self.assertEqual(frame["revision"], "2")
        self.assertEqual(frame["supersedes_request_id"], REQUEST)
        self.assertEqual(frame["artifacts"], artifact)
        self.assertTrue(frame["message_id"])
        self.assertEqual(frame["progress_policy"], "none")
        self.assertIn("Do not send acknowledgement", message)

    def test_early_v2_frame_without_message_id_remains_readable(self):
        raw = json.loads(self.request()[len(comm.PREFIX) :].splitlines()[0])
        raw.pop("message_id")
        frame = next(comm.frames({"text": comm.PREFIX + json.dumps(raw)}))
        self.assertIsNone(frame["message_id"])
        self.assertEqual(frame["request_id"], REQUEST)
        self.assertEqual(frame["scope_id"], SCOPE)

    def test_v2_request_can_opt_in_to_progress_on_change(self):
        message = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "long task",
            progress_policy="on-change",
        )
        frame = next(comm.frames({"text": message}))
        self.assertEqual(frame["progress_policy"], "on-change")
        self.assertIn("state meaningfully changes", message)

    def test_native_send_preserves_literals_without_shell_or_config_override(self):
        body = '中文 "quote" apostrophe\' $() `tick`\nsecond line'
        message = self.request(body=body)
        fake = SimpleNamespace(
            returncode=0,
            stdout=f"Queued message {MESSAGE} for thread {PEER}.\n",
            stderr="",
        )
        with (
            patch.object(comm.shutil, "which", return_value="codex.exe"),
            patch.object(comm.subprocess, "run", return_value=fake) as run,
        ):
            receipt = comm.send_native(
                PEER, message, correlation=REQUEST, sender=MAIN, kind="request"
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["codex.exe", "queue", "--thread", PEER])
        self.assertEqual(argv[-1], message)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertEqual(next(comm.frames({"text": message}))["body"], body)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(
            receipt["message_id"],
            next(comm.frames({"text": message}))["message_id"],
        )
        self.assertEqual(receipt["native_message_id"], MESSAGE)

    def test_uncertain_send_is_never_retried(self):
        message = self.request()
        logical_message_id = next(comm.frames({"text": message}))["message_id"]
        with (
            patch.object(comm.shutil, "which", return_value="codex.exe"),
            patch.object(
                comm.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("codex", 25),
            ) as run,
            self.assertRaises(comm.SendUncertain) as cm,
        ):
            comm.send_native(
                PEER,
                message,
                correlation=REQUEST,
                sender=MAIN,
                kind="request",
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(cm.exception.message_id, logical_message_id)

    def test_message_id_deduplicates_an_exact_replay(self):
        message_id = "stable-message-id"
        first = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "same request",
            scope_id=SCOPE,
            revision="1",
            message_id=message_id,
        )
        second = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "same request",
            scope_id=SCOPE,
            revision="1",
            message_id=message_id,
        )
        self.queue(PEER, first, item_id="first-copy")
        self.history(PEER, second)
        result = self.check(verbose=True)
        self.assertEqual(result["counts"]["requests"], 1)
        self.assertEqual(result["records"]["requests"][0]["message_id"], message_id)

    def test_message_id_reuse_with_conflicting_content_fails_closed(self):
        first = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "first body",
            message_id="conflicting-message-id",
        )
        second = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "different body",
            message_id="conflicting-message-id",
        )
        self.queue(PEER, first, item_id="first")
        self.history(PEER, second)
        with self.assertRaisesRegex(comm.CommError, "conflicting"):
            self.check()

    def test_delivery_progress_and_terminal_reply_lifecycle(self):
        request = self.request()
        self.queue(PEER, request)
        self.assertEqual(self.check()["status"], "queued")
        self.history(PEER, request)
        self.assertEqual(self.check()["status"], "request_observed")
        self.queue(MAIN, self.progress("acknowledged"), item_id="progress")
        progress = self.check(include_text=True)
        self.assertEqual(progress["status"], "acknowledged")
        self.assertFalse(progress["terminal"])
        self.assertEqual(progress["latest_progress"]["body"], "Working.")
        self.queue(MAIN, self.reply(body="private terminal"), item_id="reply")
        result = self.check()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["terminal"])
        self.assertNotIn("private terminal", json.dumps(result))
        self.assertEqual(
            self.check(include_text=True)["reply"]["body"], "private terminal"
        )

    def test_first_terminal_reply_wins_and_exact_repeats_are_duplicates(self):
        self.history(PEER, self.request())
        first = comm.frame_message(
            "reply",
            PEER,
            MAIN,
            REQUEST,
            "stable result",
            status="completed",
            scope_id=SCOPE,
            revision="1",
            message_id="terminal-first",
        )
        duplicate = comm.frame_message(
            "reply",
            PEER,
            MAIN,
            REQUEST,
            "stable result",
            status="completed",
            scope_id=SCOPE,
            revision="1",
            message_id="terminal-repeat",
        )
        self.queue(MAIN, first, item_id="terminal-first-native")
        self.history(MAIN, duplicate)
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["effective"])
        self.assertEqual(result["reply"]["message_id"], "terminal-first")
        self.assertEqual(result["reply"]["body"], "stable result")
        self.assertEqual(result["duplicate_terminal_count"], 1)
        self.assertEqual(result["terminal_conflict_count"], 0)

    def test_conflicting_terminal_reply_is_reported_without_overwriting_first(self):
        self.history(PEER, self.request())
        first = comm.frame_message(
            "reply",
            PEER,
            MAIN,
            REQUEST,
            "first result",
            status="completed",
            scope_id=SCOPE,
            revision="1",
            message_id="terminal-first",
        )
        conflict = comm.frame_message(
            "reply",
            PEER,
            MAIN,
            REQUEST,
            "late conflicting result",
            status="failed",
            scope_id=SCOPE,
            revision="1",
            message_id="terminal-conflict",
        )
        self.queue(MAIN, first, item_id="terminal-first-native")
        self.history(MAIN, conflict)
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "terminal_conflict")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])
        self.assertEqual(result["reply"]["message_id"], "terminal-first")
        self.assertEqual(result["reply"]["body"], "first result")
        self.assertEqual(result["terminal_conflict_count"], 1)

    def test_conflicting_cancelled_replies_require_review(self):
        self.history(PEER, self.request())
        with patch.object(
            comm,
            "_sent_at",
            side_effect=[
                "2026-09-06T20:00:01Z",
                "2026-09-06T20:00:02Z",
                "2026-09-06T20:00:03Z",
            ],
        ):
            cancel = self.control("cancel")
            first = self.reply(status="cancelled", body="first cancellation")
            conflict = self.reply(status="cancelled", body="different cancellation")
        self.history(PEER, cancel)
        self.queue(MAIN, first, item_id="cancelled-first")
        self.history(MAIN, conflict)
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "terminal_conflict")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])
        self.assertEqual(result["reply"]["body"], "first cancellation")
        self.assertEqual(result["terminal_conflict_count"], 1)

    def test_conflicting_superseded_replies_require_review(self):
        self.history(PEER, self.request())
        first = self.reply(status="superseded", body="first superseded")
        conflict = self.reply(status="superseded", body="different superseded")
        self.queue(MAIN, first, item_id="superseded-first")
        self.history(MAIN, conflict)
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "terminal_conflict")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])
        self.assertEqual(result["reply"]["body"], "first superseded")
        self.assertEqual(result["terminal_conflict_count"], 1)

    def test_notice_lifecycle_is_observable_without_a_reply(self):
        notice = comm.frame_message(
            "notice",
            MAIN,
            PEER,
            REQUEST,
            "Informational only.",
            scope_id=SCOPE,
            revision="1",
        )
        self.queue(PEER, notice)
        queued = self.check()
        self.assertEqual(queued["status"], "notice_queued")
        self.assertFalse(queued["terminal"])
        self.history(PEER, notice)
        observed = self.check()
        self.assertEqual(observed["status"], "notice_observed")
        self.assertTrue(observed["terminal"])
        self.assertEqual(observed["counts"]["notices"], 1)

    def test_queue_to_history_projection_is_logically_deduplicated(self):
        request = self.request()
        reply = self.reply()
        self.history(PEER, request)
        self.queue(MAIN, reply, item_id="queue-reply")
        self.history(MAIN, reply)
        result = self.check(verbose=True)
        self.assertEqual(result["counts"]["replies"], 1)
        record = result["records"]["replies"][0]
        self.assertEqual(set(record["locations"]), {"native_queue", "visible_history"})

    def test_supersede_is_queued_then_observed_and_old_reply_becomes_stale(self):
        self.history(PEER, self.request())
        self.queue(MAIN, self.reply(), item_id="old-reply")
        replacement = self.request(
            REPLACEMENT,
            revision="2",
            supersedes=REQUEST,
            body="new revision",
        )
        self.queue(PEER, replacement, item_id="replacement")
        queued = self.check()
        self.assertEqual(queued["status"], "supersede_queued")
        self.assertFalse(queued["terminal"])
        self.assertFalse(queued["effective"])
        self.assertIsNone(queued["reply"])
        self.assertEqual(queued["stale_reply_count"], 1)
        self.history(PEER, replacement)
        observed = self.check(include_text=True)
        self.assertEqual(observed["status"], "superseded")
        self.assertTrue(observed["terminal"])
        self.assertFalse(observed["effective"])
        self.assertEqual(observed["superseded_by"], REPLACEMENT)
        self.assertIsNone(observed["reply"])
        self.assertEqual(observed["stale_reply_count"], 1)
        self.assertNotIn("Done.", json.dumps(observed))

    def test_close_control_suppresses_late_terminal_body(self):
        self.history(PEER, self.request())
        self.queue(MAIN, self.reply(body="late body"), item_id="late")
        self.queue(
            PEER,
            self.control("close", outcome="accepted"),
            item_id="close",
        )
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["closed_outcome"], "accepted")
        self.assertFalse(result["effective"])
        self.assertIsNone(result["reply"])
        self.assertEqual(result["stale_reply_count"], 1)
        self.assertNotIn("late body", json.dumps(result))

    def test_cancel_lifecycle_distinguishes_queue_observation_and_reply(self):
        self.history(PEER, self.request())
        control = self.control("cancel")
        self.queue(PEER, control, item_id="cancel")
        self.assertEqual(self.check()["status"], "cancel_queued")
        self.history(PEER, control)
        self.assertEqual(self.check()["status"], "cancel_observed")
        self.queue(MAIN, self.reply(status="cancelled"), item_id="cancelled")
        result = self.check()
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])

    def test_cancel_after_terminal_is_a_late_noop(self):
        self.history(PEER, self.request())
        with patch.object(
            comm,
            "_sent_at",
            side_effect=[
                "2026-09-06T20:00:01Z",
                "2026-09-06T20:00:02Z",
                "2026-09-06T20:00:03Z",
            ],
        ):
            completed = self.reply(status="completed", body="completed first")
            cancel = self.control("cancel")
            cancelled = self.reply(status="cancelled", body="late cancellation")
        self.queue(MAIN, completed, item_id="completed-first")
        self.history(PEER, cancel)
        self.queue(MAIN, cancelled, item_id="cancelled-late")
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["terminal"])
        self.assertTrue(result["effective"])
        self.assertTrue(result["late_cancel_ignored"])
        self.assertEqual(result["reply"]["body"], "completed first")
        self.assertEqual(result["stale_reply_count"], 1)
        self.assertEqual(result["terminal_conflict_count"], 0)

    def test_cancel_before_terminal_accepts_only_cancelled_reply(self):
        self.history(PEER, self.request())
        with patch.object(
            comm,
            "_sent_at",
            side_effect=[
                "2026-09-06T20:00:01Z",
                "2026-09-06T20:00:02Z",
                "2026-09-06T20:00:03Z",
            ],
        ):
            cancel = self.control("cancel")
            stale_completed = self.reply(status="completed", body="ignored result")
            cancelled = self.reply(status="cancelled", body="cancelled")
        self.history(PEER, cancel)
        self.queue(MAIN, stale_completed, item_id="completed-after-cancel")
        self.history(MAIN, cancelled)
        result = self.check(include_text=True)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])
        self.assertFalse(result["late_cancel_ignored"])
        self.assertIsNone(result["reply"])
        self.assertEqual(result["stale_reply_count"], 1)
        self.assertEqual(result["terminal_conflict_count"], 0)

    def test_receiver_auto_direction_observes_inbound_close(self):
        request = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "inbound",
            scope_id=SCOPE,
            revision="1",
        )
        close = comm.frame_message(
            "control",
            PEER,
            MAIN,
            REQUEST,
            "closed",
            scope_id=SCOPE,
            revision="1",
            action="close",
            outcome="accepted",
        )
        self.history(MAIN, request)
        self.history(MAIN, close)
        result = comm.check_exchange(self.store, MAIN, PEER, REQUEST)
        self.assertEqual(result["direction"], "inbound")
        self.assertEqual(result["coordinator"], PEER)
        self.assertEqual(result["worker"], MAIN)
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])

    def test_receiver_auto_direction_observes_inbound_cancel(self):
        request = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "inbound",
            scope_id=SCOPE,
            revision="1",
        )
        cancel = comm.frame_message(
            "control",
            PEER,
            MAIN,
            REQUEST,
            "cancel",
            scope_id=SCOPE,
            revision="1",
            action="cancel",
        )
        self.history(MAIN, request)
        self.queue(MAIN, cancel, item_id="inbound-cancel")
        queued = comm.check_exchange(self.store, MAIN, PEER, REQUEST)
        self.assertEqual(queued["direction"], "inbound")
        self.assertEqual(queued["status"], "cancel_queued")
        self.history(MAIN, cancel)
        observed = comm.check_exchange(self.store, MAIN, PEER, REQUEST)
        self.assertEqual(observed["status"], "cancel_observed")
        self.assertFalse(observed["effective"])

    def test_receiver_auto_direction_observes_inbound_supersede(self):
        old = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "old",
            scope_id=SCOPE,
            revision="1",
        )
        new = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REPLACEMENT,
            "new",
            scope_id=SCOPE,
            revision="2",
            supersedes=REQUEST,
        )
        self.history(MAIN, old)
        self.history(MAIN, new)
        result = comm.check_exchange(self.store, MAIN, PEER, REQUEST)
        self.assertEqual(result["direction"], "inbound")
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(result["superseded_by"], REPLACEMENT)
        self.assertTrue(result["terminal"])
        self.assertFalse(result["effective"])

    def test_ambiguous_direction_requires_explicit_choice(self):
        self.history(PEER, self.request())
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "same id inbound",
            scope_id=SCOPE,
            revision="1",
        )
        self.history(MAIN, inbound)
        with self.assertRaisesRegex(comm.CommError, "ambiguous"):
            comm.check_exchange(self.store, MAIN, PEER, REQUEST)
        outbound = comm.check_exchange(
            self.store, MAIN, PEER, REQUEST, direction="outbound"
        )
        inbound_result = comm.check_exchange(
            self.store, MAIN, PEER, REQUEST, direction="inbound"
        )
        self.assertEqual(outbound["direction"], "outbound")
        self.assertEqual(outbound["status"], "request_observed")
        self.assertEqual(inbound_result["direction"], "inbound")
        self.assertEqual(inbound_result["status"], "request_observed")

    def test_wrong_endpoint_correlation_kind_and_reasoning_are_ignored(self):
        cases = [
            comm.frame_message("reply", OTHER, MAIN, REQUEST, "wrong sender"),
            comm.frame_message("reply", PEER, OTHER, REQUEST, "wrong target"),
            comm.frame_message("reply", PEER, MAIN, "other", "wrong id"),
            comm.frame_message("request", PEER, MAIN, "other", "not this request"),
        ]
        for index, message in enumerate(cases):
            self.queue(MAIN, message, item_id=f"wrong-{index}")
        self.history(MAIN, self.reply(), "reasoning")
        self.assertEqual(self.check()["status"], "not_observed")

    def test_reply_cannot_request_ack_loop_and_malformed_frames_are_ignored(self):
        valid = json.loads(self.reply()[len(comm.PREFIX) :].splitlines()[0])
        invalid = dict(valid)
        invalid["expects_reply"] = True
        self.queue(MAIN, comm.PREFIX + json.dumps(invalid), item_id="loop")
        for index, (key, value) in enumerate(
            (("kind", []), ("request_id", None), ("from", []), ("body", {}))
        ):
            frame = dict(valid)
            frame[key] = value
            self.queue(MAIN, comm.PREFIX + json.dumps(frame), item_id=f"bad-{index}")
        self.assertEqual(self.check()["status"], "not_observed")

    def test_peer_command_receipt_matches_return_leg(self):
        self.history(PEER, self.request())
        self.queue(MAIN, self.reply(), item_id=MESSAGE)
        item = {
            "type": "commandExecution",
            "status": "completed",
            "exitCode": 0,
            "command": "codex queue " + REQUEST,
            "aggregatedOutput": (
                json.dumps(
                    {
                        "status": "queued",
                        "kind": "reply",
                        "request_id": REQUEST,
                        "from": PEER,
                        "to": MAIN,
                        "message_id": "logical-reply-message",
                        "native_message_id": MESSAGE,
                    }
                )
                + "\n"
            ),
        }
        self.execute(
            "thread_history",
            "INSERT INTO thread_items VALUES (?,?,?,?,?,1)",
            (PEER, "peer-turn", "command-1", "commandExecution", json.dumps(item)),
        )
        self.assertEqual(self.check()["source_receipts"][0]["message_id"], MESSAGE)

    def test_inbound_request_infers_response_target_and_revision(self):
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "respond",
            scope_id=SCOPE,
            revision="7",
        )
        self.history(MAIN, inbound)
        target, request = comm._target_for_response(
            self.store, MAIN, REQUEST, None, None
        )
        self.assertEqual(target["id"], PEER)
        self.assertEqual(request["scope_id"], SCOPE)
        self.assertEqual(request["revision"], "7")
        with self.assertRaisesRegex(comm.CommError, "matched 0"):
            comm._target_for_response(self.store, MAIN, REQUEST, OTHER, None)

    def test_artifact_metadata_hashes_workspace_file_and_rejects_sensitive_paths(self):
        artifact = self.root / "handoff.md"
        artifact.write_text("result", encoding="utf-8")
        metadata = comm.artifact_metadata([artifact], workspace=self.root)
        self.assertEqual(metadata[0]["path"], "handoff.md")
        self.assertEqual(metadata[0]["bytes"], 6)
        self.assertEqual(metadata[0]["sha256"], hashlib_sha("result"))
        env = self.root / ".env"
        env.write_text("secret", encoding="utf-8")
        with self.assertRaisesRegex(comm.CommError, "Sensitive"):
            comm.artifact_metadata([env], workspace=self.root)
        outside = self.root.parent / "outside-agent-artifact.txt"
        outside.write_text("x", encoding="utf-8")
        self.addCleanup(outside.unlink)
        with self.assertRaisesRegex(comm.CommError, "workspace"):
            comm.artifact_metadata([outside], workspace=self.root)

    def test_inline_body_limits_require_artifact_or_explicit_override(self):
        comm._body_limit("reply", "x" * 4_000, False)
        with self.assertRaisesRegex(comm.CommError, "artifact"):
            comm._body_limit("reply", "x" * 4_001, False)
        comm._body_limit("reply", "x" * 4_001, True)
        with self.assertRaisesRegex(comm.CommError, "artifact"):
            comm._body_limit("progress", "x" * 1_201, False)

    def test_cli_send_emits_revision_supersede_and_recipient_metadata(self):
        self.history(PEER, self.request())
        receipt = {
            "status": "queued",
            "kind": "request",
            "request_id": REPLACEMENT,
            "from": MAIN,
            "to": PEER,
            "message_id": MESSAGE,
        }
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native", return_value=receipt) as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--supersedes",
                    REQUEST,
                    "--text",
                    "new",
                ]
            )
        self.assertEqual(code, 0)
        frame = next(comm.frames({"text": send.call_args.args[1]}))
        self.assertEqual(frame["supersedes_request_id"], REQUEST)
        self.assertEqual(frame["revision"], "2")
        self.assertEqual(frame["progress_policy"], "none")
        result = json.loads(output.getvalue())
        self.assertEqual(result["recipient"]["name"], "Reviewer")
        self.assertEqual(result["progress_policy"], "none")

    def test_cli_rejects_a_second_active_request_for_the_same_peer_and_scope(self):
        self.history(PEER, self.request())
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--text",
                    "overlapping request",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("active request", output.getvalue())
        send.assert_not_called()

    def test_cli_keeps_cancel_queued_request_active_for_scope_admission(self):
        self.history(PEER, self.request())
        self.queue(PEER, self.control("cancel"), item_id="cancel-queued")
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--text",
                    "must wait for terminal cancellation",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("active request", output.getvalue())
        send.assert_not_called()

    def test_cli_keeps_cancel_observed_request_active_for_scope_admission(self):
        self.history(PEER, self.request())
        self.history(PEER, self.control("cancel"))
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--text",
                    "must wait for terminal cancellation",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("active request", output.getvalue())
        send.assert_not_called()

    def test_cli_rejects_superseding_a_request_from_another_scope(self):
        self.history(PEER, self.request(scope_id="OTHER-SCOPE"))
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--supersedes",
                    REQUEST,
                    "--text",
                    "wrong scope",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("keep the superseded request scope", output.getvalue())
        send.assert_not_called()

    def test_cli_exact_replay_is_an_idempotent_noop(self):
        message_id = "replay-message-id"
        existing = comm.frame_message(
            "request",
            MAIN,
            PEER,
            REQUEST,
            "same request",
            scope_id=SCOPE,
            revision="1",
            message_id=message_id,
        )
        self.queue(PEER, existing, item_id="native-existing")
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REQUEST,
                    "--message-id",
                    message_id,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "1",
                    "--text",
                    "same request",
                ]
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "already_present")
        self.assertEqual(result["message_id"], message_id)
        send.assert_not_called()

    def test_cli_request_id_reuse_with_a_new_message_id_is_rejected(self):
        self.queue(PEER, self.request(), item_id="native-existing")
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REQUEST,
                    "--scope-id",
                    SCOPE,
                    "--text",
                    "replacement under reused ID",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("request_id already exists", output.getvalue())
        send.assert_not_called()

    def test_cli_terminal_replay_is_an_idempotent_noop(self):
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "reply once",
            scope_id=SCOPE,
            revision="1",
        )
        self.history(MAIN, inbound)
        message_id = "terminal-replay-message"
        existing = comm.frame_message(
            "reply",
            MAIN,
            PEER,
            REQUEST,
            "stable terminal",
            status="completed",
            scope_id=SCOPE,
            revision="1",
            message_id=message_id,
        )
        self.queue(PEER, existing, item_id="native-terminal")
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "reply",
                    "--request-id",
                    REQUEST,
                    "--message-id",
                    message_id,
                    "--status",
                    "completed",
                    "--text",
                    "stable terminal",
                ]
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "already_present")
        self.assertEqual(result["message_id"], message_id)
        send.assert_not_called()

    def test_cli_allows_new_same_scope_work_after_terminal_reply(self):
        self.history(PEER, self.request())
        self.queue(MAIN, self.reply(), item_id="terminal")
        receipt = {
            "status": "queued",
            "kind": "request",
            "request_id": REPLACEMENT,
            "from": MAIN,
            "to": PEER,
            "message_id": "next-message",
            "native_message_id": MESSAGE,
        }
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native", return_value=receipt) as send,
            redirect_stdout(io.StringIO()),
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REPLACEMENT,
                    "--scope-id",
                    SCOPE,
                    "--revision",
                    "2",
                    "--text",
                    "next task",
                ]
            )
        self.assertEqual(code, 0)
        send.assert_called_once()

    def test_cli_uncertain_send_reports_the_stable_message_id(self):
        message_id = "uncertain-message-id"
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm.shutil, "which", return_value="codex.exe"),
            patch.object(
                comm.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("codex", 25),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REQUEST,
                    "--message-id",
                    message_id,
                    "--scope-id",
                    SCOPE,
                    "--text",
                    "uncertain request",
                ]
            )
        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "send_uncertain")
        self.assertEqual(result["message_id"], message_id)
        self.assertEqual(result["request_id"], REQUEST)

    def test_cli_send_can_opt_in_to_progress_on_change(self):
        receipt = {
            "status": "queued",
            "kind": "request",
            "request_id": REQUEST,
            "from": MAIN,
            "to": PEER,
            "message_id": MESSAGE,
        }
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native", return_value=receipt) as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                [
                    "send",
                    "--to",
                    PEER,
                    "--request-id",
                    REQUEST,
                    "--progress-policy",
                    "on-change",
                    "--text",
                    "long task",
                ]
            )
        self.assertEqual(code, 0)
        frame = next(comm.frames({"text": send.call_args.args[1]}))
        self.assertEqual(frame["progress_policy"], "on-change")
        self.assertEqual(json.loads(output.getvalue())["progress_policy"], "on-change")

    def test_cli_progress_rejects_quiet_request(self):
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "quiet task",
        )
        self.history(MAIN, inbound)
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                ["progress", "--request-id", REQUEST, "--status", "acknowledged"]
            )
        self.assertEqual(code, 2)
        self.assertIn("progress_policy=none", output.getvalue())
        send.assert_not_called()

    def test_cli_progress_allowed_when_request_opted_in(self):
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "long task",
            progress_policy="on-change",
        )
        self.history(MAIN, inbound)
        receipt = {
            "status": "queued",
            "kind": "progress",
            "request_id": REQUEST,
            "from": MAIN,
            "to": PEER,
            "message_id": MESSAGE,
        }
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native", return_value=receipt) as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = comm.main(
                ["progress", "--request-id", REQUEST, "--status", "waiting"]
            )
        self.assertEqual(code, 0)
        frame = next(comm.frames({"text": send.call_args.args[1]}))
        self.assertEqual(frame["kind"], "progress")
        self.assertEqual(frame["status"], "waiting")
        self.assertEqual(json.loads(output.getvalue())["progress_policy"], "on-change")

    def test_cli_reply_infers_target_and_preserves_request_metadata(self):
        inbound = comm.frame_message(
            "request",
            PEER,
            MAIN,
            REQUEST,
            "respond",
            scope_id=SCOPE,
            revision="9",
        )
        self.history(MAIN, inbound)
        receipt = {
            "status": "queued",
            "kind": "reply",
            "request_id": REQUEST,
            "from": MAIN,
            "to": PEER,
            "message_id": MESSAGE,
        }
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native", return_value=receipt) as send,
            redirect_stdout(io.StringIO()),
        ):
            code = comm.main(
                ["reply", "--request-id", REQUEST, "--status", "completed"]
            )
        self.assertEqual(code, 0)
        frame = next(comm.frames({"text": send.call_args.args[1]}))
        self.assertEqual(frame["to"], PEER)
        self.assertEqual(frame["scope_id"], SCOPE)
        self.assertEqual(frame["revision"], "9")

    def test_cli_rejects_invalid_utf8_before_sending(self):
        file = self.root / "invalid.txt"
        file.write_bytes(b"\xff\xfe")
        with (
            patch.object(comm, "codex_home", return_value=self.root),
            patch.object(comm, "current_thread", return_value=MAIN),
            patch.object(comm, "send_native") as send,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = comm.main(["send", "--to", PEER, "--file", str(file)])
        self.assertEqual(result, 2)
        self.assertIn("UTF-8", output.getvalue())
        send.assert_not_called()

    def test_compact_check_omits_records_and_verbose_is_opt_in(self):
        self.history(PEER, self.request(body="private request"))
        compact = self.check()
        self.assertNotIn("records", compact)
        self.assertNotIn("private request", json.dumps(compact))
        verbose = self.check(verbose=True)
        self.assertIn("records", verbose)
        self.assertNotIn("private request", json.dumps(verbose))


def hashlib_sha(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
