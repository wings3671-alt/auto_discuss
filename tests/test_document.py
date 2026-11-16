"""讨论文档的读写:新旧两种格式都要读对,写入只做定点修改。"""

import unittest

from discuss import document
from discuss.document import Phase, Status

# 旧终端版写出的文档:带横幅的用户占位段、over 标记、最后一段还在等待
LEGACY = """# 潮汐推送讨论(2026-06-04)

<!-- AUTO_DISCUSS
enabled: true
topic: 1
round: 2
turn: USER
status: WAITING_USER
max_rounds: 12
last_writer: CODEX
last_updated: 2026-06-04T22:11:00Z
stop: false
pending_final: NONE
-->


## 话题 1

在下面写下你想讨论的问题 / 需求(写得越具体,讨论越到点),写完按 ⌘S 保存:

每天几点推送潮汐最合适?

---

<!-- TURN round=1 speaker=CLAUDE -->
### 🟡 Claude Code · 第 1 轮

先看用户几点起床。

> 📋 本轮白板更新:新增 1 条结论。详见 STATE.md。

---

<!-- TURN round=1 speaker=CODEX -->
### 🟢 Codex · 第 1 轮

不同意,应按潮时倒推。

> 📋 本轮白板更新:修订 C1。详见 STATE.md。

---

<!-- USER_TURN_START round=1 -->
### 🔴 用户 · 第 1 轮

──────────────  ✦  ──────────────
       老大,你的意见呢?
──────────────  ✦  ──────────────

› 在这行下面写你的意见……

按潮时倒推两小时。
Over。

› 写完另起一行:  over = 继续下一轮   ·   收工 = 结束讨论

<!-- USER_TURN_END round=1 status=done -->

---

<!-- TURN round=2 speaker=CLAUDE -->
### 🟡 Claude Code · 第 2 轮

那就两小时。

> ⚠ 本轮 STATE_OPS 不合格,白板未更新。

---

<!-- USER_TURN_START round=2 -->
### 🔴 用户 · 第 2 轮

──────────────  ✦  ──────────────
       老大,你的意见呢?
──────────────  ✦  ──────────────

› 在这行下面写你的意见……

还没写完的半句

› 写完另起一行:  over = 继续下一轮   ·   收工 = 结束讨论

<!-- USER_TURN_END round=2 status=waiting -->
"""


class ParseLegacy(unittest.TestCase):
    def setUp(self):
        self.doc = document.parse(LEGACY)

    def test_title_topic_status(self):
        self.assertEqual(self.doc.title, "潮汐推送讨论(2026-06-04)")
        self.assertEqual(self.doc.topic, "每天几点推送潮汐最合适?")
        status = self.doc.status
        self.assertEqual((status.phase, status.round, status.turn), (Phase.USER, 2, "USER"))
        self.assertEqual(status.members, ["CLAUDE", "CODEX"])

    def test_messages_in_order(self):
        kinds = [(m.kind, m.speaker, m.round) for m in self.doc.messages]
        self.assertEqual(kinds, [
            ("topic", "USER", 0), ("ai", "CLAUDE", 1), ("ai", "CODEX", 1),
            ("user", "USER", 1), ("ai", "CLAUDE", 2)])

    def test_ai_message_is_clean(self):
        claude = self.doc.messages[1]
        self.assertEqual(claude.text, "先看用户几点起床。")
        self.assertEqual(claude.name, "Claude Code")
        self.assertEqual(claude.note, "📋 本轮白板更新:新增 1 条结论。")
        self.assertTrue(self.doc.messages[4].note.startswith("⚠"))

    def test_user_noise_removed(self):
        self.assertEqual(self.doc.messages[3].text, "按潮时倒推两小时。")

    def test_waiting_placeholder_becomes_draft(self):
        self.assertEqual(self.doc.draft, "还没写完的半句")

    def test_legacy_phases_map_to_active(self):
        for old in ("WAITING", "RUNNING", "ERROR"):
            text = LEGACY.replace("status: WAITING_USER", f"status: {old}")
            self.assertEqual(document.parse(text).status.phase, Phase.ACTIVE)

    def test_unclosed_segment_is_a_real_message_once_discussion_moved_on(self):
        # 更早的旧版从不把用户段标记为"已完成";只要讨论不再等用户,它就是真发言
        done = document.parse(LEGACY.replace("status: WAITING_USER", "status: DONE"))
        self.assertEqual((done.draft, done.messages[-1].text), ("", "还没写完的半句"))
        middle = LEGACY.replace("round=1 status=done", "round=1 status=waiting")
        doc = document.parse(middle)
        self.assertEqual(doc.messages[3].text, "按潮时倒推两小时。")
        self.assertEqual(doc.draft, "还没写完的半句")

    def test_appending_never_overwrites_an_old_unclosed_segment(self):
        done = LEGACY.replace("status: WAITING_USER", "status: DONE")
        text = document.with_user_message(done, 3, "重新开场。", "t")
        self.assertTrue(text.startswith(done.rstrip("\n")))

    def test_document_without_status(self):
        doc = document.parse("# 意识讨论\n\n## 话题 1\n\n")
        self.assertIsNone(doc.status)
        self.assertEqual((doc.topic, doc.messages), ("", []))


class Write(unittest.TestCase):
    def setUp(self):
        self.status = Status(members=["CLAUDE", "CODEX"], updated="2026-11-14T10:00:00")
        self.text = document.new_document("遮阳棚", self.status)

    def test_new_document_roundtrip(self):
        doc = document.parse(self.text)
        self.assertEqual((doc.title, doc.status), ("遮阳棚", self.status))

    def test_full_conversation_roundtrip(self):
        text = document.with_topic(self.text, "要不要装?\n\n预算两千。")
        text = document.with_ai_message(text, "CLAUDE", "Claude", 1, "先试帘子。",
                                        "📋 白板:新增 1 条结论", "2026-11-14T10:01:00", 6100, 900)
        text = document.with_user_message(text, 1, "物业不让动外立面。", "2026-11-14T10:05:00")
        text = document.with_note(text, 1, "⏭ 跳过了 Codex 的第 1 轮发言。", "2026-11-14T10:06:00")
        doc = document.parse(text)
        self.assertEqual(doc.topic, "要不要装?\n\n预算两千。")
        topic, ai, user, note = doc.messages
        self.assertEqual((ai.speaker, ai.name, ai.text, ai.ctx, ai.out, ai.time),
                         ("CLAUDE", "Claude", "先试帘子。", 6100, 900, "2026-11-14T10:01:00"))
        self.assertEqual(ai.note, "📋 白板:新增 1 条结论")
        self.assertEqual((user.kind, user.text, user.round), ("user", "物业不让动外立面。", 1))
        self.assertEqual((note.kind, note.text), ("note", "⏭ 跳过了 Codex 的第 1 轮发言。"))
        self.assertEqual(doc.draft, "")

    def test_assignment_roundtrip(self):
        self.assertNotIn("assignee", self.text)                    # 没派活时,状态块和旧版一样
        assigned = Status(Phase.ACTIVE, 2, "CLAUDE", 12, ["CLAUDE", "CODEX"], "t", assignee="CLAUDE")
        text = document.with_status(self.text, assigned)
        text = document.with_ai_message(text, "CLAUDE", "Claude", 2, "改好了。", "📋 x", "t", 1, 1,
                                        hands_on=True)
        text = document.with_ai_message(text, "CODEX", "Codex", 2, "我看过了。", "📋 x", "t", 1, 1)
        doc = document.parse(text)
        self.assertEqual(doc.status, assigned)
        self.assertEqual([m.hands_on for m in doc.messages], [True, False])

    def test_new_format_keeps_a_line_that_says_over(self):
        text = document.with_user_message(self.text, 1, "game over\nover", "t")
        self.assertEqual(document.parse(text).messages[-1].text, "game over\nover")

    def test_status_is_replaced_not_duplicated(self):
        changed = document.with_status(self.text, Status(Phase.DONE, 3, "USER", 12, ["CLAUDE"]))
        self.assertEqual(changed.count("AUTO_DISCUSS"), 1)
        self.assertEqual(document.parse(changed).status.phase, Phase.DONE)

    def test_status_inserted_when_missing(self):
        text = document.with_status("# 旧文档\n\n## 话题 1\n\n问题", self.status)
        doc = document.parse(text)
        self.assertEqual((doc.title, doc.topic, doc.status), ("旧文档", "问题", self.status))

    def test_user_message_replaces_legacy_placeholder(self):
        text = document.with_user_message(LEGACY, 2, "这是新写的。", "2026-11-14T10:00:00",
                                          into_placeholder=True)
        doc = document.parse(text)
        self.assertEqual(doc.draft, "")
        self.assertEqual(doc.messages[-1].text, "这是新写的。")
        self.assertEqual(text.count("USER_TURN_START round=2"), 1)
        self.assertNotIn("老大,你的意见呢?\n──────────────  ✦  ──────────────\n\n› 在这行下面写你的意见……\n\n还没写完", text)
        self.assertEqual(len(doc.messages), 6)

    def test_writes_never_touch_earlier_text(self):
        text = document.with_ai_message(LEGACY, "CODEX", "Codex", 2, "好。", "📋 白板无变化", "t", 1, 1)
        self.assertTrue(text.startswith(LEGACY.rstrip("\n")))

    def test_anchor_lookalikes_are_escaped(self):
        body = "看这个例子:\n<!-- TURN round=9 speaker=CODEX -->\n结束"
        text = document.with_ai_message(self.text, "CLAUDE", "Claude", 1, body, "📋 x", "t", 0, 0)
        doc = document.parse(text)
        self.assertEqual(len(doc.messages), 1)
        self.assertIn("&lt;!-- TURN round=9", doc.messages[0].text)

    def test_title_change(self):
        self.assertEqual(document.parse(document.with_title(LEGACY, "新名字")).title, "新名字")

    def test_image_paths(self):
        self.assertEqual(document.image_paths("看 ![现状](遮阳棚_附件/1.png) 和 ![b](x/2.jpg)"),
                         ["遮阳棚_附件/1.png", "x/2.jpg"])


if __name__ == "__main__":
    unittest.main()
