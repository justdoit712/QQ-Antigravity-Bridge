import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from history_store import ConversationKey, HistoryError, HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".local" / "history.sqlite3"
        self.store = HistoryStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.conversation = ConversationKey("app", "owner", "c2c", "friend")

    def reopen(self):
        self.store.close()
        self.store = HistoryStore(self.path)

    def append_turn(self, index, conversation=None):
        conversation = conversation or self.conversation
        return self.store.append(
            conversation, f"message-{index}", f"用户消息 {index}", f"回答 {index}", "test-model",
        )

    def expected_context(self, indexes):
        history = []
        for index in indexes:
            history.extend([
                {"role": "user", "content": f"用户消息 {index}"},
                {"role": "assistant", "content": f"回答 {index}"},
            ])
        return history

    def test_reopening_restores_latest_six_turns_and_keeps_full_archive(self):
        for index in range(10):
            self.assertTrue(self.append_turn(index))

        self.reopen()

        self.assertEqual(self.store.load_context(self.conversation), self.expected_context(range(4, 10)))
        turns, total = self.store.page(self.conversation)
        self.assertEqual(total, 10)
        self.assertEqual([turn.user_text for turn in turns], [f"用户消息 {index}" for index in range(9, 4, -1)])
        self.assertTrue(all(turn.created_at > 0 for turn in turns))
        self.assertTrue(all(turn.model == "test-model" for turn in turns))

    def test_restored_context_obeys_character_limit_and_keeps_complete_turns(self):
        expected = []
        for index in range(4):
            user = f"问题 {index} " + "问" * 5000
            answer = f"回答 {index} " + "答" * 2000
            self.store.append(self.conversation, f"long-message-{index}", user, answer, "test-model")
            expected.extend([
                {"role": "user", "content": user},
                {"role": "assistant", "content": answer},
            ])

        self.reopen()

        context = self.store.load_context(self.conversation)
        self.assertEqual(context, expected[2:])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 24000)
        self.assertGreater(len(json.dumps(expected, ensure_ascii=False)), 24000)
        self.assertEqual(self.store.page(self.conversation)[1], 4)

    def test_all_namespace_fields_isolate_context_archive_and_message_ids(self):
        conversations = [
            self.conversation,
            replace(self.conversation, app_id="other-app"),
            replace(self.conversation, owner_id="other-owner"),
            replace(self.conversation, scope="group"),
            replace(self.conversation, chat_id="other-friend"),
        ]
        for index, conversation in enumerate(conversations):
            self.assertFalse(self.store.contains(conversation, "shared-message-id"))
            self.assertTrue(self.store.append(
                conversation, "shared-message-id", f"问题 {index}", f"回答 {index}", f"model-{index}",
            ))

        self.reopen()

        for index, conversation in enumerate(conversations):
            with self.subTest(conversation=conversation):
                self.assertTrue(self.store.contains(conversation, "shared-message-id"))
                self.assertEqual(self.store.load_context(conversation), [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": f"回答 {index}"},
                ])
                turns, total = self.store.page(conversation)
                self.assertEqual(total, 1)
                self.assertEqual(turns[0].model, f"model-{index}")
                for other in conversations:
                    if other != conversation:
                        self.assertIsNone(self.store.get(other, turns[0].id))

    def test_reset_persists_context_boundary_and_preserves_archive(self):
        self.append_turn(0)
        old_turn = self.store.page(self.conversation)[0][0]

        self.assertTrue(self.store.reset_context(self.conversation))
        self.reopen()

        self.assertEqual(self.store.load_context(self.conversation), [])
        self.assertEqual(self.store.get(self.conversation, old_turn.id), old_turn)
        self.assertEqual(self.store.page(self.conversation)[1], 1)
        self.assertTrue(self.store.contains(self.conversation, "message-0"))

        self.append_turn(1)
        self.reopen()

        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([1]))
        self.assertEqual(self.store.page(self.conversation)[1], 2)

    def test_resetting_empty_conversation_allows_future_context(self):
        self.store.reset_context(self.conversation)
        self.reopen()
        self.assertEqual(self.store.load_context(self.conversation), [])

        self.append_turn(0)
        self.reopen()

        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([0]))

    def test_reset_command_replay_preserves_context_added_after_reset(self):
        self.append_turn(0)
        self.assertTrue(self.store.reset_context(self.conversation, message_id="reset-command"))
        self.assertTrue(self.store.contains(self.conversation, "reset-command"))
        self.append_turn(1)
        self.reopen()

        self.assertFalse(self.store.reset_context(self.conversation, message_id="reset-command"))
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([1]))
        self.assertEqual(self.store.page(self.conversation)[1], 2)
        self.reopen()
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([1]))

    def test_clear_deletes_archive_and_context_while_preventing_replay(self):
        self.append_turn(0)
        self.append_turn(1)
        old_ids = [turn.id for turn in self.store.page(self.conversation)[0]]

        self.assertTrue(self.store.clear(self.conversation))
        self.reopen()

        self.assertEqual(self.store.page(self.conversation), ([], 0))
        self.assertEqual(self.store.load_context(self.conversation), [])
        for turn_id in old_ids:
            self.assertIsNone(self.store.get(self.conversation, turn_id))
        for index in range(2):
            self.assertTrue(self.store.contains(self.conversation, f"message-{index}"))
            self.assertFalse(self.append_turn(index))
        self.assertEqual(self.store.page(self.conversation), ([], 0))

        self.append_turn(2)
        self.reopen()

        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([2]))
        self.assertEqual(self.store.page(self.conversation)[1], 1)

    def test_clear_command_replay_preserves_archive_added_after_clear(self):
        self.append_turn(0)
        self.assertTrue(self.store.clear(self.conversation, message_id="clear-command"))
        self.assertTrue(self.store.contains(self.conversation, "clear-command"))
        self.append_turn(1)
        self.reopen()

        self.assertFalse(self.store.clear(self.conversation, message_id="clear-command"))
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([1]))
        self.assertEqual(self.store.page(self.conversation)[1], 1)
        self.reopen()
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([1]))
        self.assertTrue(self.store.contains(self.conversation, "clear-command"))

    def test_group_clear_records_command_in_administrator_source_conversation(self):
        source = replace(self.conversation, chat_id="owner")
        target = replace(self.conversation, scope="group", chat_id="group-a")
        other_target = replace(target, chat_id="group-b")
        self.append_turn(0, source)
        self.append_turn(1, target)
        self.append_turn(2, other_target)

        self.assertTrue(self.store.clear(target, source=source, message_id="admin-clear"))
        self.reopen()

        self.assertTrue(self.store.contains(source, "admin-clear"))
        self.assertFalse(self.store.contains(target, "admin-clear"))
        self.assertEqual(self.store.page(target), ([], 0))
        self.assertEqual(self.store.load_context(source), self.expected_context([0]))
        self.assertFalse(self.store.clear(other_target, source=source, message_id="admin-clear"))
        self.assertEqual(self.store.load_context(other_target), self.expected_context([2]))
        self.assertEqual(self.store.page(other_target)[1], 1)

    def test_control_receipts_survive_normal_replay_marker_eviction(self):
        for action_name in ("reset_context", "clear"):
            with self.subTest(action=action_name):
                target = replace(self.conversation, chat_id=action_name)
                self.append_turn(0, target)
                action = getattr(self.store, action_name)
                self.assertTrue(action(target, message_id="old-control-command"))
                for index in range(1001):
                    self.store.append(target, f"new-message-{index}", f"新问题 {index}", "新回答", "model")
                self.reopen()
                expected_context = self.store.load_context(target)
                expected_archive = self.store.page(target)

                self.assertTrue(self.store.contains(target, "old-control-command"))
                self.assertFalse(self.store.append(
                    target, "old-control-command", "重复内容", "重复回答", "model"))
                action = getattr(self.store, action_name)
                self.assertFalse(action(target, message_id="old-control-command"))
                self.assertEqual(self.store.load_context(target), expected_context)
                self.assertEqual(self.store.page(target), expected_archive)

    def test_failed_control_marker_rolls_back_reset_and_clear(self):
        source = replace(self.conversation, chat_id="owner")
        for action_name in ("reset_context", "clear"):
            with self.subTest(action=action_name):
                target = replace(self.conversation, scope="group", chat_id=action_name)
                self.append_turn(0, target)
                original_turns = self.store.page(target)
                with closing(sqlite3.connect(self.path)) as database:
                    database.execute("""
                        CREATE TRIGGER reject_control_marker
                        BEFORE INSERT ON control_receipts
                        BEGIN
                            SELECT RAISE(ABORT, 'synthetic-control-marker-error');
                        END;
                    """)
                    database.commit()

                action = getattr(self.store, action_name)
                with self.assertRaises(HistoryError) as error:
                    action(target, source=source, message_id=action_name)

                self.assertNotIn("synthetic-control-marker-error", str(error.exception))
                self.reopen()
                self.assertEqual(self.store.page(target), original_turns)
                self.assertEqual(self.store.load_context(target), self.expected_context([0]))
                self.assertFalse(self.store.contains(source, action_name))
                with closing(sqlite3.connect(self.path)) as database:
                    database.execute("DROP TRIGGER reject_control_marker")
                    database.commit()
                action = getattr(self.store, action_name)
                self.assertTrue(action(target, source=source, message_id=action_name))
                self.assertTrue(self.store.contains(source, action_name))
                self.assertEqual(self.store.load_context(target), [])
                self.assertEqual(self.store.page(target)[1], 0 if action_name == "clear" else 1)

    def test_reset_and_clear_leave_other_conversations_untouched(self):
        other = replace(self.conversation, chat_id="other-friend")
        self.append_turn(0)
        self.append_turn(1, other)
        other_turn = self.store.page(other)[0][0]

        self.store.reset_context(self.conversation)
        self.assertEqual(self.store.load_context(other), self.expected_context([1]))
        self.store.clear(self.conversation)
        self.reopen()

        self.assertEqual(self.store.load_context(other), self.expected_context([1]))
        self.assertEqual(self.store.get(other, other_turn.id), other_turn)
        self.assertTrue(self.store.contains(other, "message-1"))

    def test_replay_protection_is_bounded_after_archive_deletion(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.executemany(
                "INSERT INTO processed_messages VALUES (?, ?, ?, ?, ?)",
                [(*self.conversation.values, f"old-message-{index}") for index in range(1000)],
            )
            database.commit()

        self.append_turn(0)
        self.store.clear(self.conversation)
        self.reopen()

        self.assertFalse(self.store.contains(self.conversation, "old-message-0"))
        self.assertTrue(self.store.contains(self.conversation, "old-message-1"))
        self.assertTrue(self.store.contains(self.conversation, "old-message-999"))
        self.assertTrue(self.store.contains(self.conversation, "message-0"))
        with closing(sqlite3.connect(self.path)) as database:
            count = database.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
        self.assertEqual(count, 1000)

    def test_append_is_idempotent_and_preserves_original_turn(self):
        self.assertTrue(self.append_turn(0))
        original = self.store.page(self.conversation)[0][0]
        self.assertFalse(self.store.append(
            self.conversation, "message-0", "替换问题", "替换回答", "replacement-model",
        ))
        self.reopen()

        self.assertFalse(self.append_turn(0))
        self.assertEqual(self.store.page(self.conversation), ([original], 1))
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([0]))

    def test_page_returns_five_recent_turns_and_consistent_total(self):
        for index in range(12):
            self.append_turn(index)

        for page, indexes in [(1, range(11, 6, -1)), (2, range(6, 1, -1)), (3, range(1, -1, -1)), (4, [])]:
            with self.subTest(page=page):
                turns, total = self.store.page(self.conversation, page=page)
                self.assertEqual(total, 12)
                self.assertEqual([turn.user_text for turn in turns], [f"用户消息 {index}" for index in indexes])
                self.assertEqual([turn.id for turn in turns], sorted([turn.id for turn in turns], reverse=True))

    def test_keyword_matches_literal_substrings_in_user_and_assistant_text(self):
        keyword = "中文100%_literal"
        self.store.append(self.conversation, "user-match", f"问题包含{keyword}", "普通回答", "model")
        self.store.append(self.conversation, "assistant-match", "普通问题", f"回答包含{keyword}", "model")
        self.store.append(self.conversation, "wildcard-lookalike", "中文100xxliteral", "其他内容", "model")
        other = replace(self.conversation, owner_id="other-owner")
        self.store.append(other, "other-owner", keyword, keyword, "model")

        turns, total = self.store.page(self.conversation, keyword=keyword)

        self.assertEqual(total, 2)
        self.assertEqual([turn.user_text for turn in turns], ["普通问题", f"问题包含{keyword}"])
        self.assertEqual(self.store.page(self.conversation, keyword="找不到的文字"), ([], 0))

    def test_keyword_is_parameterized_and_filtered_results_are_paginated(self):
        keyword = "' OR 1=1 --"
        for index in range(7):
            self.store.append(self.conversation, f"match-{index}", f"{keyword} {index}", "回答", "model")
        self.append_turn("unmatched")

        first_page, total = self.store.page(self.conversation, keyword=keyword)
        second_page, second_total = self.store.page(self.conversation, page=2, keyword=keyword)

        self.assertEqual(total, 7)
        self.assertEqual(second_total, 7)
        self.assertEqual([turn.user_text for turn in first_page], [f"{keyword} {index}" for index in range(6, 1, -1)])
        self.assertEqual([turn.user_text for turn in second_page], [f"{keyword} 1", f"{keyword} 0"])

    def test_page_rejects_nonpositive_page_numbers(self):
        for page in (0, -1):
            with self.subTest(page=page), self.assertRaisesRegex(ValueError, "页码"):
                self.store.page(self.conversation, page=page)

    def test_get_returns_full_turn_and_rejects_other_conversation(self):
        self.append_turn(0)
        turn = self.store.page(self.conversation)[0][0]

        self.assertEqual(self.store.get(self.conversation, turn.id), turn)
        self.assertIsNone(self.store.get(self.conversation, turn.id + 1))
        self.assertIsNone(self.store.get(replace(self.conversation, chat_id="other-chat"), turn.id))

    def test_corrupt_database_error_omits_file_contents_and_path(self):
        self.store.close()
        sensitive_text = "synthetic-private-database-content"
        self.path.write_text(sensitive_text, encoding="utf-8")

        with self.assertRaises(HistoryError) as error:
            HistoryStore(self.path)

        self.assertNotIn(sensitive_text, str(error.exception))
        self.assertNotIn(str(self.path), str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_failed_append_rolls_back_turn_and_replay_marker(self):
        sensitive_error = "synthetic-private-transaction-error"
        with closing(sqlite3.connect(self.path)) as database:
            database.execute(f"""
                CREATE TRIGGER reject_processed_message
                BEFORE INSERT ON processed_messages
                BEGIN
                    SELECT RAISE(ABORT, '{sensitive_error}');
                END;
            """)
            database.commit()

        with self.assertRaises(HistoryError) as error:
            self.append_turn(0)

        self.assertNotIn(sensitive_error, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual(self.store.page(self.conversation), ([], 0))
        self.assertEqual(self.store.load_context(self.conversation), [])
        self.assertFalse(self.store.contains(self.conversation, "message-0"))
        self.reopen()
        self.assertEqual(self.store.page(self.conversation), ([], 0))
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("DROP TRIGGER reject_processed_message")
            database.commit()

        self.assertTrue(self.append_turn(0))
        self.assertEqual(self.store.load_context(self.conversation), self.expected_context([0]))

    def test_closed_store_reports_safe_error(self):
        self.store.close()

        with self.assertRaisesRegex(HistoryError, "已经关闭"):
            self.store.page(self.conversation)

    @unittest.skipIf(os.name == "nt", "Requires POSIX permissions")
    def test_database_and_created_directory_have_private_permissions(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    @unittest.skipIf(os.name == "nt", "Requires POSIX permissions")
    def test_opening_existing_database_repairs_permissive_file_mode(self):
        self.store.close()
        self.path.chmod(0o644)

        self.store = HistoryStore(self.path)

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
