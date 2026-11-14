"""状态白板:文件读写往返一致;STATE_OPS 严格校验;编号只增不复用。"""

import unittest

from discuss import whiteboard
from discuss.whiteboard import Board, BoardError


def ops_reply(json_text: str, speech: str = "我的发言。") -> str:
    return f"{speech}\n\n```STATE_OPS\n{json_text}\n```"


class BoardFile(unittest.TestCase):
    def test_roundtrip(self):
        board = Board()
        whiteboard.apply(board, [
            {"op": "add", "section": "conclusions", "text": "只装内侧"},
            {"op": "add", "section": "constraints", "text": "预算两千"}], "r1-claude")
        again = whiteboard.parse(whiteboard.render(board))
        self.assertEqual(again, board)
        self.assertEqual(again.sections["conclusions"][0].id, "C1")
        self.assertEqual(again.sections["constraints"][0].touched, "r1-claude")

    def test_reads_file_written_by_old_version(self):
        old = ("<!-- STATE_META next_C=1 next_Q=9 next_R=1 next_K=5 -->\n# 讨论滚动状态\n\n"
               "## 待解决的问题\n- [Q1] 访问范围待定   <!-- touched: r1-codex -->\n\n"
               "## 关键约束 / 前提\n- [K2] 低运维   <!-- touched: r1-claude -->\n")
        board = whiteboard.parse(old)
        self.assertEqual(board.counters, {"C": 1, "Q": 9, "R": 1, "K": 5})
        self.assertEqual(board.ids(), {"Q1", "K2"})

    def test_missing_meta_is_an_error(self):
        with self.assertRaises(BoardError):
            whiteboard.parse("# 讨论滚动状态\n")

    def test_ids_are_never_reused(self):
        board = Board()
        add = {"op": "add", "section": "open_questions", "text": "问题"}
        whiteboard.apply(board, [add], "r1-a")
        whiteboard.apply(board, [{"op": "remove", "id": "Q1"}], "r1-a")
        whiteboard.apply(board, [add], "r2-a")
        self.assertEqual(board.ids(), {"Q2"})

    def test_update_changes_text_and_author(self):
        board = Board()
        whiteboard.apply(board, [{"op": "add", "section": "rejected", "text": "旧"}], "r1-a")
        whiteboard.apply(board, [{"op": "update", "id": "R1", "text": " 新 "}], "r2-b")
        item = board.sections["rejected"][0]
        self.assertEqual((item.text, item.touched), ("新", "r2-b"))

    def test_token_estimate(self):
        self.assertEqual(whiteboard.estimate_tokens("中文四字"), 4)
        self.assertEqual(whiteboard.estimate_tokens("abcdefgh"), 2)


class Ops(unittest.TestCase):
    def setUp(self):
        self.board = Board()
        whiteboard.apply(self.board, [
            {"op": "add", "section": "open_questions", "text": "问题"}], "r1-a")

    def test_valid_reply(self):
        ops = whiteboard.read_ops(ops_reply('{"ops": [{"op": "remove", "id": "Q1"}]}'), self.board)
        self.assertEqual(ops, [{"op": "remove", "id": "Q1"}])

    def test_empty_ops_is_valid(self):
        self.assertEqual(whiteboard.read_ops(ops_reply('{"ops": []}'), self.board), [])

    def test_rejections(self):
        bad = {
            "没有代码块": "只有发言",
            "两个代码块": ops_reply('{"ops": []}') + "\n" + ops_reply('{"ops": []}', ""),
            "不是 JSON": ops_reply("{ops: []}"),
            "多余的键": ops_reply('{"ops": [], "note": 1}'),
            "ops 不是数组": ops_reply('{"ops": {}}'),
            "未知操作": ops_reply('{"ops": [{"op": "move", "id": "Q1"}]}'),
            "栏目非法": ops_reply('{"ops": [{"op": "add", "section": "ideas", "text": "x"}]}'),
            "正文为空": ops_reply('{"ops": [{"op": "add", "section": "rejected", "text": " "}]}'),
            "多余字段": ops_reply('{"ops": [{"op": "remove", "id": "Q1", "text": "x"}]}'),
            "编号不存在": ops_reply('{"ops": [{"op": "remove", "id": "Q7"}]}'),
            "编号格式错": ops_reply('{"ops": [{"op": "remove", "id": 1}]}'),
            "同号引用两次": ops_reply('{"ops": [{"op": "remove", "id": "Q1"},'
                                ' {"op": "update", "id": "Q1", "text": "x"}]}'),
        }
        for label, reply in bad.items():
            with self.subTest(label), self.assertRaises(BoardError):
                whiteboard.read_ops(reply, self.board)

    def test_split_reply_removes_block(self):
        body, blocks = whiteboard.split_reply(ops_reply('{"ops": []}', "第一段。\n\n第二段。"))
        self.assertEqual((body, blocks), ("第一段。\n\n第二段。", ['{"ops": []}']))

    def test_visible_part_hides_unfinished_block(self):
        self.assertEqual(whiteboard.visible_part('发言中…\n\n```STATE_OPS\n{"ops": ['), "发言中…")
        self.assertEqual(whiteboard.visible_part("还没到指令"), "还没到指令")

    def test_summary(self):
        ops = [{"op": "add", "section": "conclusions", "text": "a"},
               {"op": "add", "section": "conclusions", "text": "b"},
               {"op": "update", "id": "Q1", "text": "c"},
               {"op": "remove", "id": "K2"}]
        self.assertEqual(whiteboard.summarize(ops), "白板:新增 2 条结论;修订 Q1;删除 K2")
        self.assertEqual(whiteboard.summarize([]), "白板无变化")


if __name__ == "__main__":
    unittest.main()
