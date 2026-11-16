"""调度器:用假的 AI 把整场讨论的各种走法跑一遍。不联网、不碰真实数据。"""

import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from discuss import document
from discuss.config import Paths
from discuss.document import Phase
from discuss.engine import Engine, EngineError
from discuss.providers import ProviderError, Reply, TurnCancelled

logging.disable(logging.CRITICAL)      # 测试里故意制造的失败不必刷屏

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GOOD_OPS = '```STATE_OPS\n{"ops": [{"op": "add", "section": "conclusions", "text": "一条结论"}]}\n```'
NO_OPS = '```STATE_OPS\n{"ops": []}\n```'


class FakeAI:
    """按剧本作答的假 AI。剧本是 {成员编号: [回复或异常, ...]},用完后答默认内容。
    和真实情况一样,只有 Claude 类型的成员能被派活。"""

    sees_images = True
    reads_files = True

    def __init__(self, member, script, calls):
        self.member, self.script, self.calls = member, script, calls
        self.assignable = member.kind == "claude"

    def run(self, request):
        self.calls.append((self.member.id, request))
        queue = self.script.get(self.member.id, [])
        answer = queue.pop(0) if queue else f"{self.member.name} 的发言。\n\n{NO_OPS}"
        if answer == "BLOCK":                       # 一直说不完,直到被用户叫停
            request.cancel.wait(10)
            raise TurnCancelled()
        if isinstance(answer, Exception):
            raise answer
        request.on_text(answer)
        return Reply(answer, ctx=1234, out=56)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = Paths(root, trash=root / "废纸篓", downloads=root / "下载")
        self.paths.ensure()
        # 关键隔离:测试绝不向真实 NAS 同步
        self.paths.settings.write_text(json.dumps({
            "nas_sync": True, "nas_mount": str(root / "没挂载的NAS"),
            "nas_dir": str(root / "没挂载的NAS" / "备份")}), encoding="utf-8")
        self.script, self.calls = {}, []
        self.revealed = []
        self.engine = Engine(self.paths, lambda member: FakeAI(member, self.script, self.calls),
                             reveal=self.revealed.append)

    def idle(self, sid):
        deadline = time.monotonic() + 5
        while self.engine._session(sid).busy:
            self.assertLess(time.monotonic(), deadline, "后台任务一直没结束")
            time.sleep(0.01)
        return self.engine.view(sid)

    def started(self, topic="要不要装遮阳棚?"):
        sid = self.engine.create("遮阳棚")
        self.engine.send(sid, topic)
        return sid, self.idle(sid)

    def speakers(self, view):
        return [(m["kind"], m["speaker"], m["round"]) for m in view["messages"]]


class Flow(EngineCase):
    def test_new_discussion_waits_for_topic(self):
        sid = self.engine.create("  遮阳棚  方案 ")
        view = self.engine.view(sid)
        self.assertEqual((view["title"], view["phase"], view["messages"]), ("遮阳棚 方案", "new", []))
        with self.assertRaises(EngineError):
            self.engine.send(sid, "   ")

    def test_first_round_runs_until_user(self):
        self.script["CLAUDE"] = [f"先试帘子。\n\n{GOOD_OPS}"]
        sid, view = self.started()
        self.assertEqual(view["phase"], "user")
        self.assertEqual(self.speakers(view), [
            ("topic", "USER", 0), ("ai", "CLAUDE", 1), ("ai", "CODEX", 1)])
        claude = view["messages"][1]
        self.assertEqual((claude["text"], claude["ctx"], claude["out"]), ("先试帘子。", 1234, 56))
        self.assertEqual(claude["note"], "📋 白板:新增 1 条结论")
        self.assertEqual(view["board"]["sections"][0]["items"][0]["text"], "一条结论")
        self.assertEqual(view["health"]["CLAUDE"]["ctx"], 1234)

    def test_each_ai_sees_board_others_and_user(self):
        self.script["CLAUDE"] = [f"甲的看法。\n\n{GOOD_OPS}"]
        sid, _ = self.started("议题正文")
        self.engine.send(sid, "用户的决定")
        self.idle(sid)
        prompts = {(who, i): request.prompt for i, (who, request) in enumerate(self.calls)}
        codex_first = prompts[("CODEX", 1)]
        self.assertIn("议题正文", codex_first)
        self.assertIn("甲的看法。", codex_first)                 # 对方上一次发言
        self.assertIn("[C1] 一条结论", codex_first)               # 白板
        self.assertIn("用户的决定", prompts[("CLAUDE", 2)])       # 第 2 轮带上用户发言
        self.assertNotIn("STATE_OPS\n{\"ops\": [{\"op\": \"add\"", prompts[("CLAUDE", 2)].split("【你这一轮要做的】")[0])

    def test_user_over_then_finish(self):
        sid, _ = self.started()
        self.engine.send(sid, "继续")
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["round"]), ("user", 2))
        self.engine.send(sid, "", finish=True)
        view = self.idle(sid)
        self.assertEqual(view["phase"], "done")
        self.assertEqual(view["messages"][-1]["text"], "收工。")

    def test_finish_queues_sync_when_nas_is_offline(self):
        sid, _ = self.started()
        self.engine.send(sid, "好", finish=True)
        deadline = time.monotonic() + 5
        while self.engine.view(sid)["sync"]["state"] != "pending":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertFalse((Path(self.tmp.name) / "没挂载的NAS").exists())   # 没挂载就绝不创建目录

    def test_finish_copies_discussion_to_nas_when_mounted(self):
        target = Path(self.tmp.name) / "NAS备份"
        settings = self.engine.settings_view()
        settings.update(nas_mount="/", nas_dir=str(target))     # "/" 一定是挂载点,借它模拟已连接的 NAS
        self.engine.update_settings(settings)
        sid = self.engine.create("遮阳棚")
        image = self.engine.attach(sid, "a.png", PNG)
        self.engine.send(sid, f"议题 {image['markdown']}")
        self.idle(sid)
        self.engine.send(sid, "好", finish=True)
        deadline = time.monotonic() + 10
        while self.engine.view(sid)["sync"]["state"] != "synced":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        source = self.paths.discussions
        for name in (f"{sid}.md", f"{sid}.state.md", f"{sid}_附件/{image['name']}"):
            self.assertEqual((target / name).read_bytes(), (source / name).read_bytes(), name)

    def test_reopen_finished_discussion(self):
        sid, _ = self.started()
        self.engine.send(sid, "先到这", finish=True)
        self.idle(sid)
        self.engine.send(sid, "又想到一点")
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["round"]), ("user", 2))
        self.assertEqual(self.speakers(view)[-3:], [
            ("user", "USER", 2), ("ai", "CLAUDE", 2), ("ai", "CODEX", 2)])

    def test_round_limit_asks_before_extending(self):
        sid, _ = self.started()
        raw = self.engine._session(sid).path
        status = document.parse(raw.read_text("utf-8")).status
        status.max_rounds = 1
        raw.write_text(document.with_status(raw.read_text("utf-8"), status), "utf-8")
        answer = self.engine.send(sid, "继续")
        self.assertTrue(answer["need_extend"])
        self.assertEqual(self.idle(sid)["phase"], "user")           # 什么都没改
        self.engine.send(sid, "继续", extend=True)
        view = self.idle(sid)
        self.assertEqual((view["round"], view["max_rounds"]), (2, 7))


class Trouble(EngineCase):
    def test_bad_ops_retried_once_then_soft_fallback(self):
        self.script["CLAUDE"] = ["没带指令的发言一。", "没带指令的发言二。"]
        sid, view = self.started()
        claude = view["messages"][1]
        self.assertEqual(claude["text"], "没带指令的发言二。")
        self.assertTrue(claude["note"].startswith("⚠ 白板未更新"))
        self.assertIn("【重要纠正】", self.calls[1][1].prompt)
        self.assertEqual(view["phase"], "user")                      # 讨论照常继续

    def test_error_pauses_and_resume_retries(self):
        self.script["CODEX"] = [ProviderError("Codex 的登录已过期。")]
        sid, view = self.started()
        self.assertEqual((view["phase"], view["turn"]), ("paused", "CODEX"))
        self.assertEqual(view["error"], "Codex 的登录已过期。")
        self.assertEqual(self.speakers(view)[-1], ("ai", "CLAUDE", 1))    # 已说的没丢
        self.engine.resume(sid)
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["error"]), ("user", ""))

    def test_skip_moves_on_and_leaves_a_note(self):
        self.script["CLAUDE"] = [ProviderError("坏了")]
        sid, _ = self.started()
        self.engine.skip(sid)
        view = self.idle(sid)
        self.assertEqual(view["phase"], "user")
        self.assertEqual(self.speakers(view), [
            ("topic", "USER", 0), ("note", "", 1), ("ai", "CODEX", 1)])
        self.assertIn("跳过了 Claude", view["messages"][1]["text"])

    def test_stop_cancels_running_turn(self):
        self.script["CLAUDE"] = ["BLOCK"]
        sid = self.engine.create("遮阳棚")
        self.engine.send(sid, "议题")
        self.assertEqual(self.engine.view(sid)["phase"], "running")
        with self.assertRaises(EngineError) as blocked:
            self.engine.send(sid, "插一句")
        self.assertEqual(blocked.exception.code, 409)
        self.engine.stop(sid)
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["error"]), ("paused", "你停止了这一棒。"))

    def test_interrupt_and_add_restarts_the_same_turn(self):
        self.script["CLAUDE"] = ["BLOCK"]
        sid = self.engine.create("遮阳棚")
        self.engine.send(sid, "议题")
        self.engine.send(sid, "等等,预算只有两千", interrupt=True)
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["round"], view["error"]), ("user", 1, ""))
        self.assertEqual(self.speakers(view), [
            ("topic", "USER", 0), ("user", "USER", 1), ("ai", "CLAUDE", 1), ("ai", "CODEX", 1)])
        self.assertIn("等等,预算只有两千", self.calls[1][1].prompt)       # 重说时带上了补充

    def test_interjection_while_paused_keeps_round(self):
        self.script["CLAUDE"] = [ProviderError("坏了")]
        sid, _ = self.started()
        self.engine.send(sid, "补充一个条件")
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["round"]), ("user", 1))
        self.assertEqual(self.speakers(view)[1], ("user", "USER", 1))
        self.assertIn("补充一个条件", self.calls[-1][1].prompt)

    def test_removed_member_gives_clear_error(self):
        sid = self.engine.create("遮阳棚")
        settings = self.engine.settings_view()
        settings["members"] = [m for m in settings["members"] if m["id"] != "CLAUDE"]
        self.engine.update_settings(settings)
        self.engine.send(sid, "议题")
        view = self.idle(sid)
        self.assertEqual(view["phase"], "paused")
        self.assertIn("已不在成员名单里", view["error"])
        self.assertTrue(view["people"]["CLAUDE"]["missing"])


class Assigning(EngineCase):
    """派活:用户点名一位成员,只有它、只在那一棒能动手。"""

    def requests(self, member_id):
        return [request for who, request in self.calls if who == member_id]

    def test_only_the_assignee_gets_hands_on(self):
        sid, _ = self.started()
        self.engine.send(sid, "把 a.txt 改成 b", assignee="CLAUDE")
        view = self.idle(sid)
        claude, codex = self.requests("CLAUDE")[-1], self.requests("CODEX")[-1]
        self.assertTrue(claude.hands_on)
        self.assertEqual(claude.protected, [self.paths.discussions, self.paths.data])
        self.assertEqual((claude.workdir, claude.timeout), (Path.home(), 1800))
        self.assertIn("【派活守则 —— 这一棒你能改文件、跑命令", claude.prompt)
        self.assertIn(str(self.paths.discussions), claude.prompt)        # 告诉它哪里不许改
        self.assertFalse(codex.hands_on)
        self.assertEqual((codex.protected, codex.workdir, codex.timeout),
                         ([], self.paths.discussions, 600))
        self.assertIn("这是派给 Claude 的活,不是派给你的", codex.prompt)
        self.assertIn("【派活守则 —— Claude 这一棒必须遵守;你审查时对照它】", codex.prompt)
        rules = "- 只做 我 明确吩咐的事。"                                # 两边拿到同一份条文
        self.assertEqual(claude.prompt.split(rules)[1].split("STATE_OPS 代码块 ——")[0],
                         codex.prompt.split(rules)[1].split("STATE_OPS 代码块 ——")[0])
        self.assertEqual([m["hands_on"] for m in view["messages"][-2:]], [True, False])
        self.assertEqual(view["assignee"], "")                           # 这一回结束,派活也结束

    def test_next_round_is_plain_talk_again(self):
        sid, _ = self.started()
        self.engine.send(sid, "干活", assignee="CLAUDE")
        self.idle(sid)
        self.engine.send(sid, "接着聊")
        self.idle(sid)
        self.assertFalse(self.requests("CLAUDE")[-1].hands_on)
        self.assertNotIn("派给", self.requests("CODEX")[-1].prompt)

    def test_first_message_can_assign(self):
        sid = self.engine.create("遮阳棚")
        self.engine.send(sid, "量一下阳台尺寸", assignee="CLAUDE")
        self.idle(sid)
        self.assertTrue(self.requests("CLAUDE")[0].hands_on)

    def test_assignee_sees_its_own_last_speech(self):
        self.script["CLAUDE"] = [f"我打算删掉旧文件,可以吗?\n\n{NO_OPS}"]
        sid, _ = self.started()
        self.engine.send(sid, "可以,删吧", assignee="CLAUDE")
        self.idle(sid)
        self.assertIn("【你自己上一次的发言】\n我打算删掉旧文件,可以吗?",
                      self.requests("CLAUDE")[-1].prompt)
        self.assertNotIn("【你自己上一次的发言】", self.requests("CODEX")[-1].prompt)

    def test_only_assignable_members_of_this_discussion(self):
        sid, _ = self.started()
        for assignee, reason in (("CODEX", "不能被派活"), ("NOBODY", "不在这场讨论的成员里")):
            with self.assertRaises(EngineError) as refused:
                self.engine.send(sid, "干活", assignee=assignee)
            self.assertIn(reason, str(refused.exception))
        view = self.engine.view(sid)
        self.assertEqual((view["phase"], view["round"]), ("user", 1))    # 被拒绝的发言没有写进去
        self.assertEqual([view["people"][who]["assignable"] for who in ("CLAUDE", "CODEX")],
                         [True, False])

    def test_hands_on_turn_is_never_rerun_for_bad_ops(self):
        sid, _ = self.started()
        self.script["CLAUDE"] = ["活干完了,但没带白板指令。"]
        before = len(self.requests("CLAUDE"))
        self.engine.send(sid, "干活", assignee="CLAUDE")
        view = self.idle(sid)
        self.assertEqual(len(self.requests("CLAUDE")) - before, 1)       # 重来等于把活再干一遍
        report = view["messages"][-2]
        self.assertEqual(report["text"], "活干完了,但没带白板指令。")
        self.assertTrue(report["note"].startswith("⚠ 白板未更新"))

    def test_failed_job_stays_assigned_for_retry_and_interjection(self):
        sid, _ = self.started()
        self.script["CLAUDE"] = [ProviderError("断网了"), ProviderError("又断了")]
        self.engine.send(sid, "干活", assignee="CLAUDE")
        view = self.idle(sid)
        self.assertEqual((view["phase"], view["turn"], view["assignee"]),
                         ("paused", "CLAUDE", "CLAUDE"))
        self.engine.resume(sid)                                          # 重试这一棒
        self.idle(sid)
        self.engine.send(sid, "补充:只改第一行")                          # 暂停时插话,没有重新点名
        self.idle(sid)
        self.assertEqual([request.hands_on for request in self.requests("CLAUDE")[-3:]],
                         [True, True, True])

    def test_skipping_the_assignee_cancels_the_job(self):
        sid, _ = self.started()
        self.script["CLAUDE"] = [ProviderError("断网了")]
        self.engine.send(sid, "干活", assignee="CLAUDE")
        self.idle(sid)
        self.engine.skip(sid)
        self.idle(sid)
        self.assertNotIn("派给", self.requests("CODEX")[-1].prompt)

    def test_cannot_assign_someone_whose_turn_has_passed(self):
        self.script["CODEX"] = [ProviderError("断网了")]
        sid, view = self.started()
        self.assertEqual(view["turn"], "CODEX")                          # Claude 这一回已经说完
        with self.assertRaises(EngineError) as refused:
            self.engine.send(sid, "再干点活", assignee="CLAUDE")
        self.assertIn("这一回已经发过言了", str(refused.exception))

    def test_assignment_is_recorded_in_the_document(self):
        sid, _ = self.started()
        self.script["CLAUDE"] = ["BLOCK"]
        self.engine.send(sid, "干活", assignee="CLAUDE")
        path = self.paths.discussions / f"{sid}.md"
        self.assertIn("\nassignee: CLAUDE\n", path.read_text(encoding="utf-8"))
        self.assertTrue(self.engine.summaries()[0]["hands_on"])          # 列表里显示"正在干活"
        self.engine.stop(sid)
        self.idle(sid)
        self.engine.resume(sid)
        self.idle(sid)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("assignee:", text)
        self.assertEqual(text.count(" hands_on=1 -->"), 1)


class Extras(EngineCase):
    def test_tidy_rewrites_board_and_notes_it(self):
        self.script["CLAUDE"] = [f"发言。\n\n{GOOD_OPS}"]
        sid, _ = self.started()
        self.script["CLAUDE"] = ['整理完毕。\n\n```STATE_OPS\n{"ops": [{"op": "update", "id": "C1", "text": "短"}]}\n```']
        self.engine.tidy(sid)
        view = self.idle(sid)
        self.assertEqual(view["board"]["sections"][0]["items"][0]["text"], "短")
        self.assertEqual(view["messages"][-1]["kind"], "note")
        self.assertEqual(view["phase"], "user")

    def test_png_and_jpg_are_attached_as_images(self):
        sid = self.engine.create("遮阳棚")
        saved = self.engine.attach(sid, "阳台 现状(1).png", PNG)
        self.assertTrue(self.engine.attachment(sid, saved["name"]).is_file())
        self.assertTrue(saved["image"])
        self.assertRegex(saved["markdown"], r"^!\[阳台_现状_1\]\(遮阳棚_\d{4}_附件/\d{6}_阳台_现状_1\.png\)$")

    def test_anything_else_is_attached_as_a_file(self):
        sid = self.engine.create("遮阳棚")
        saved = self.engine.attach(sid, "报价 单(终稿).PDF", b"%PDF-1.7 ...")
        self.assertEqual((saved["image"], saved["label"]), (False, "报价_单_终稿.pdf"))
        self.assertRegex(saved["markdown"],
                         r"^\[报价_单_终稿\.pdf\]\(遮阳棚_\d{4}_附件/\d{6}_报价_单_终稿\.pdf\)$")
        self.assertEqual(self.engine.attach(sid, "Makefile", b"all:")["label"], "Makefile")
        self.assertEqual(self.engine.attach(sid, "怪.名字!!", b"x")["label"], "怪")   # 不像扩展名的不留
        self.engine.reveal(sid, saved["name"])
        self.assertEqual([path.name for path in self.revealed], [saved["name"]])

    def test_only_images_are_served_to_the_page(self):
        sid = self.engine.create("遮阳棚")
        page = self.engine.attach(sid, "坏心眼.html", b"<script>alert(1)</script>")
        with self.assertRaises(EngineError) as refused:
            self.engine.attachment(sid, page["name"])
        self.assertEqual(refused.exception.code, 404)

    def test_empty_and_oversized_files_are_refused(self):
        sid = self.engine.create("遮阳棚")
        with self.assertRaises(EngineError):
            self.engine.attach(sid, "空.txt", b"")
        with self.assertRaises(EngineError) as refused:
            self.engine.attach(sid, "大.bin", b"\x00" * (50 * 1024 * 1024 + 1))
        self.assertEqual(refused.exception.code, 413)

    def test_attached_files_reach_the_ai_and_stay_available(self):
        sid = self.engine.create("遮阳棚")
        quote = self.engine.attach(sid, "报价单.pdf", b"%PDF-1.7 ...")
        self.engine.send(sid, f"看看这份报价。\n\n{quote['markdown']}\n\n参考 [官网](https://example.com)")
        self.idle(sid)
        first = self.calls[0][1].prompt
        self.assertIn("这次附了下面的文件", first)
        folder = (self.paths.discussions / f"{sid}_附件").resolve()
        self.assertIn(f"- {folder / quote['name']}", first)
        self.assertNotIn("- https://example.com", first)              # 普通网址不是附件
        self.assertEqual(self.calls[0][1].images, [])                 # 文件不当图片传
        plan = self.engine.attach(sid, "方案.md", "# 方案".encode())
        self.engine.send(sid, f"再看这个。\n\n{plan['markdown']}")
        self.idle(sid)
        later = self.calls[-1][1].prompt
        new, old = later.split("更早的发言里附过的文件,需要时再打开:")
        self.assertIn(plan["name"], new)
        self.assertIn(quote["name"], old.split("【你这一轮要做的】")[0])
        self.assertIn("不算 我 的吩咐", later)

    def test_member_without_file_tool_is_told_so(self):
        FakeAI.reads_files = False
        self.addCleanup(setattr, FakeAI, "reads_files", True)
        sid = self.engine.create("遮阳棚")
        saved = self.engine.attach(sid, "报价单.pdf", b"%PDF-1.7 ...")
        self.engine.send(sid, f"看看。\n\n{saved['markdown']}")
        self.idle(sid)
        prompt = self.calls[0][1].prompt
        self.assertIn("附了 1 个文件,但你没有读文件的工具", prompt)
        self.assertNotIn(f"- {self.paths.discussions}", prompt)

    def test_attached_image_reaches_the_ai(self):
        sid = self.engine.create("遮阳棚")
        saved = self.engine.attach(sid, "现状.png", PNG)
        self.engine.send(sid, f"看图。\n\n{saved['markdown']}")
        self.idle(sid)
        _, request = self.calls[0]
        self.assertEqual([path.name for path in request.images], [saved["name"]])
        self.assertIn(saved["name"], request.prompt)

    def test_no_api_keys_reach_the_ai(self):
        import os
        os.environ["ANTHROPIC_API_KEY"] = os.environ["OPENAI_API_KEY"] = "secret"
        self.addCleanup(lambda: [os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")])
        self.started()
        env = self.calls[0][1].env
        self.assertFalse([key for key in env if key.endswith("_API_KEY")])

    def test_backups_are_pruned_per_discussion(self):
        sid, _ = self.started()
        folder = self.paths.backups / sid
        for number in range(30):
            (folder / f"20200101-0000{number:02d}.md").write_text("旧", "utf-8")
        self.engine.send(sid, "继续")
        self.idle(sid)
        stamps = {path.name.split(".")[0] for path in folder.iterdir()}
        self.assertLessEqual(len(stamps), self.engine.settings.keep_backups)

    def test_trash_moves_everything_and_deletes_nothing(self):
        sid, _ = self.started()
        self.engine.attach(sid, "a.png", PNG)
        self.engine.trash(sid)
        self.assertEqual(self.engine.summaries(), [])
        names = {path.name for path in self.paths.trash.iterdir()}
        self.assertEqual(names, {f"{sid}.md", f"{sid}.state.md", f"{sid}_附件", f"{sid}_备份"})

    def test_trash_can_retire_the_nas_copy_instead_of_deleting_it(self):
        nas = Path(self.tmp.name) / "NAS备份"
        settings = self.engine.settings_view()
        settings.update(nas_mount="/", nas_dir=str(nas))
        self.engine.update_settings(settings)
        sid, _ = self.started()
        self.engine.send(sid, "好", finish=True)
        while self.engine.view(sid)["sync"]["state"] != "synced":
            time.sleep(0.02)
        self.assertEqual(self.engine.trash(sid, with_nas=True), {"nas": "moved"})
        retired = [path for path in nas.iterdir() if path.name.startswith("已删除_")]
        self.assertEqual(len(retired), 1)
        self.assertEqual({path.name for path in retired[0].iterdir()}, {f"{sid}.md", f"{sid}.state.md"})
        self.assertEqual([path.name for path in nas.iterdir()], [retired[0].name])   # 原处已空,没有任何东西被删

    def test_trash_reports_when_nas_is_offline_or_has_no_copy(self):
        sid, _ = self.started()
        self.assertEqual(self.engine.trash(sid, with_nas=True), {"nas": "offline"})
        sid, _ = self.started()
        self.assertEqual(self.engine.trash(sid), {"nas": "kept"})

    def test_pinned_discussions_come_first(self):
        first, _ = self.started()
        time.sleep(0.01)
        second = self.engine.create("后建的")
        self.assertEqual([row["id"] for row in self.engine.summaries()], [second, first])
        self.engine.pin(first, True)
        rows = self.engine.summaries()
        self.assertEqual([(row["id"], row["pinned"]) for row in rows], [(first, True), (second, False)])
        self.engine.pin(first, False)
        self.assertEqual(self.engine.summaries()[0]["id"], second)

    def test_search_looks_inside_messages(self):
        self.script["CLAUDE"] = [f"内侧卷帘加隔热膜是性价比最高的组合。\n\n{NO_OPS}"]
        sid, _ = self.started("要不要装?")
        other = self.engine.create("隔热膜选购")
        hits = {hit["id"]: hit["snippet"] for hit in self.engine.search(" 隔热膜 ")}
        self.assertEqual(hits[other], "")                               # 标题命中
        self.assertEqual(hits[sid], "内侧卷帘加隔热膜是性价比最高的组合。")
        self.assertEqual(self.engine.search("不存在的词"), [])
        self.assertEqual(self.engine.search("   "), [])

    def test_export_is_saved_to_downloads_and_revealed(self):
        sid, _ = self.started()
        self.engine.rename(sid, "阳台: 遮阳/方案")
        path = Path(self.engine.save_export(sid, "<p>导出</p>".encode()))
        again = Path(self.engine.save_export(sid, b"<p>2</p>"))
        self.assertEqual((path.parent, path.name), (self.paths.downloads, "阳台_遮阳_方案.html"))
        self.assertEqual(path.read_text("utf-8"), "<p>导出</p>")
        self.assertEqual(again.name, "阳台_遮阳_方案 2.html")              # 不覆盖已有文件
        self.assertEqual(self.revealed, [path, again])

    def test_rename_changes_title_not_file(self):
        sid, _ = self.started()
        self.engine.rename(sid, "新名字")
        self.assertEqual(self.engine.view(sid)["title"], "新名字")
        self.assertTrue((self.paths.discussions / f"{sid}.md").exists())

    def test_unknown_or_sneaky_ids_are_rejected(self):
        for sid in ("不存在", "../运行数据/设置", "讨论守则", ""):
            with self.assertRaises(EngineError):
                self.engine.view(sid)

    def test_settings_reject_wrong_types(self):
        self.engine.update_settings({
            "port": "8765", "max_rounds": True, "proxy": 7890, "notify": "yes",
            "members": [{"id": "claude"}, {"id": "GROK", "kind": "telepathy"},
                        {"id": "LOCAL", "name": "千问", "kind": "ollama", "model": "qwen3"}]})
        settings = self.engine.settings
        self.assertEqual((settings.port, settings.max_rounds, settings.notify), (8765, 12, True))
        self.assertEqual((settings.theme, settings.text_size), ("auto", "medium"))
        self.engine.update_settings({"theme": "dark", "text_size": "huge"})
        self.assertEqual((self.engine.settings.theme, self.engine.settings.text_size), ("dark", "medium"))
        self.assertEqual(settings.proxy, "")
        self.assertEqual([m.id for m in settings.members], ["LOCAL"])
        with self.assertRaises(EngineError):
            self.engine.update_settings({"members": [
                {"id": "LOCAL", "name": "千问", "kind": "ollama", "model": ""}]})


if __name__ == "__main__":
    unittest.main()
