import math
from types import SimpleNamespace
import unittest

from history_commands import (
    DETAIL_PAGE_SIZE,
    HistoryCommand,
    format_history_page,
    format_history_turn,
    parse_history_command,
)


def saved_turn(turn_id=1, user_text="你好", assistant_text="你好呀", model="test-model"):
    return SimpleNamespace(id=turn_id, created_at=0, user_text=user_text,
                           assistant_text=assistant_text, model=model)


class HistoryParsingTests(unittest.TestCase):
    def parse(self, text, scope="c2c"):
        return parse_history_command(text, scope, "current-chat", ["group-a", "group-b"])

    def test_current_conversation_list_search_and_detail(self):
        self.assertEqual(self.parse(" /history "), HistoryCommand("c2c", "current-chat"))
        self.assertEqual(self.parse("/history 2", "group"),
                         HistoryCommand("group", "current-chat", page=2))
        self.assertEqual(self.parse("/history search 一起  吃饭"),
                         HistoryCommand("c2c", "current-chat", "search", keyword="一起  吃饭"))
        self.assertEqual(self.parse("/history show 15 3"),
                         HistoryCommand("c2c", "current-chat", "show", page=3, turn_id=15))
        self.assertEqual(self.parse("/history show 15").page, 1)
        self.assertEqual(self.parse("/history clear").operation, "clear")

    def test_private_owner_can_select_authorized_group(self):
        self.assertEqual(self.parse("/history group 2"), HistoryCommand("group", "group-b"))
        self.assertEqual(self.parse("/history group 1 2").page, 2)
        self.assertEqual(self.parse("/history group 1 search 晚饭 地点").keyword, "晚饭 地点")
        self.assertEqual(self.parse("/history group 1 show 5 2"),
                         HistoryCommand("group", "group-a", "show", page=2, turn_id=5))
        self.assertEqual(self.parse("/history group 2 clear"),
                         HistoryCommand("group", "group-b", "clear"))

    def test_group_members_can_query_current_group(self):
        self.assertEqual(self.parse("/history search 你好", "group").chat_id, "current-chat")
        self.assertEqual(self.parse("/history show 1", "group").turn_id, 1)
        for text in ("/history group 1", "/history clear"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text, "group")
        with self.assertRaisesRegex(ValueError, "管理员私聊"):
            self.parse("/history clear", "group")

    def test_invalid_syntax_and_numbers_return_usage(self):
        invalid = ("", "/historyish", "/history extra", "/history 2 extra",
                   "/history search", "/history show", "/history show 1 2 3",
                   "/history clear extra", "/history group", "/history group 3")
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)
        for number in ("0", "-1", "+1", "１", "1.0", "1000000000", "9999999999"):
            for text in (f"/history {number}", f"/history show {number}",
                         f"/history show 1 {number}", f"/history group {number}"):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    self.parse(text)
        self.assertEqual(self.parse("/history 999999999").page, 999999999)

    def test_invalid_scope_or_missing_target_is_rejected(self):
        for scope, chat_id in (("guild", "chat"), ("c2c", "")):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                parse_history_command("/history", scope, chat_id, [])


class HistoryFormattingTests(unittest.TestCase):
    def test_summary_is_bounded_and_uses_china_time(self):
        turns = [saved_turn(i, "你 \n 好 " + "甲" * 6000, "乙" * 1800) for i in range(1, 7)]
        text = format_history_page(turns, 10, HistoryCommand("group", "a"))
        self.assertLessEqual(len(text), 1800)
        self.assertIn("第 1/2 页", text)
        self.assertIn("01-01 08:00", text)
        self.assertIn("用户　你 好 ", text)
        self.assertNotIn("#6 ", text)
        self.assertIn("/history show", text)
        for line in text.splitlines():
            if line.startswith(("用户　", "doro　")):
                self.assertLessEqual(len(line.split("　", 1)[1]), 100)

    def test_empty_search_and_out_of_range_states(self):
        command = HistoryCommand("c2c", "owner", "search", keyword="敏感搜索词" * 1000)
        self.assertIn("暂无匹配", format_history_page([], 0, command))
        text = format_history_page([saved_turn()], 1, command)
        self.assertIn("匹配记录", text)
        self.assertNotIn(command.keyword, text)
        self.assertIn("暂无聊天", format_history_page([], 0, HistoryCommand("c2c", "owner")))
        self.assertIn("页码超出", format_history_page([], 1, HistoryCommand("c2c", "owner", page=2)))

    def test_search_describes_latest_matches_without_page_count(self):
        turns = [saved_turn(i) for i in range(1, 7)]
        for command in (HistoryCommand("group", "a", "search", keyword="你好"),
                        HistoryCommand("group", "a", keyword="你好"),
                        HistoryCommand("group", "a", "search")):
            with self.subTest(command=command):
                text = format_history_page(turns, 25, command)
                self.assertIn("匹配记录共 25 轮，显示最近 5 轮（北京时间）", text)
                self.assertNotIn("1/5", text)
                self.assertNotIn("#6 ", text)
                self.assertLessEqual(len(text), 1800)
        text = format_history_page([saved_turn()], 1, HistoryCommand("group", "a", "search"))
        self.assertIn("显示最近 1 轮", text)

    def test_full_record_pages_preserve_every_character(self):
        turn = saved_turn(user_text="甲\n\n" * 2000, assistant_text="乙" * 1800, model="m" * 5000)
        body = f"用户\n{turn.user_text}\n\ndoro\n{turn.assistant_text}"
        page_count = math.ceil(len(body) / DETAIL_PAGE_SIZE)
        segments = []
        for page in range(1, page_count + 1):
            text = format_history_turn(turn, HistoryCommand("c2c", "a", "show", page=page, turn_id=1))
            self.assertLessEqual(len(text), 1800)
            self.assertIn(f"第 {page}/{page_count} 页", text)
            self.assertIn("1970-01-01 08:00", text)
            segments.append(text.split("\n\n", 1)[1].rsplit("\n\n", 1)[0])
        self.assertEqual("".join(segments), body)

    def test_detail_missing_out_of_range_and_default_model(self):
        command = HistoryCommand("c2c", "a", "show", turn_id=2)
        self.assertIn("找不到", format_history_turn(None, command))
        self.assertIn("CLI 默认模型", format_history_turn(saved_turn(model=""), command))
        self.assertIn("页码超出", format_history_turn(
            saved_turn(), HistoryCommand("c2c", "a", "show", page=2, turn_id=2)))


if __name__ == "__main__":
    unittest.main()
