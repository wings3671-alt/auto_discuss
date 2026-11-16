"""各家 AI 的调用方式里,不用真的启动 AI 就能核对的部分。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from discuss.config import Member
from discuss.providers import Claude, Codex, Request, _codex_context


def request(**extra):
    return Request(prompt="", workdir=Path("/"), env={}, timeout=1, cancel=threading.Event(), **extra)


class ClaudeCommand(unittest.TestCase):
    """权限全靠命令行参数定,这里把住关:平时只读;被派活才能写、能跑命令。"""

    def setUp(self):
        self.claude = Claude(Member("CLAUDE", "Claude", "claude"))

    def tools(self, cmd):
        allowed, usable = cmd[cmd.index("--allowedTools") + 1], cmd[cmd.index("--tools") + 1]
        self.assertEqual(allowed, usable)
        return set(usable.split(","))

    def test_talk_turn_is_read_only(self):
        cmd = self.claude.command(request())
        self.assertEqual(self.tools(cmd), {"Read", "Glob", "Grep", "WebSearch", "WebFetch"})
        self.assertNotIn("--disallowedTools", cmd)
        self.assertIn("--strict-mcp-config", cmd)

    def test_hands_on_turn_can_write_but_not_into_protected_folders(self):
        cmd = self.claude.command(request(hands_on=True, protected=[Path("/库/讨论记录"), Path("/库/运行数据")]))
        self.assertEqual(self.tools(cmd), {"Read", "Glob", "Grep", "WebSearch", "WebFetch",
                                           "Edit", "Write", "Bash"})
        denied = cmd.index("--disallowedTools")
        self.assertEqual(cmd[denied + 1:denied + 3],
                         ["Edit(//库/讨论记录/**)", "Edit(//库/运行数据/**)"])
        self.assertIn("--strict-mcp-config", cmd)                   # 动手时也不加载 MCP

    def test_model_is_passed_when_set(self):
        cmd = Claude(Member("CLAUDE", "Claude", "claude", model="haiku")).command(request())
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")


class CodexCommand(unittest.TestCase):
    """Codex 的权限是沙箱定的:平时只读;被派活才能写,而且只能写点了名的地方。"""

    def setUp(self):
        self.codex = Codex(Member("CODEX", "Codex", "codex"))

    def test_talk_turn_runs_in_the_read_only_sandbox(self):
        cmd = self.codex.command(request(writable=[Path("/外置硬盘")]))
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertNotIn("--add-dir", cmd)                           # 只读时不认额外的文件夹
        self.assertNotIn("sandbox_workspace_write.network_access=true", cmd)

    def test_hands_on_turn_can_write_the_workdir_and_named_folders_only(self):
        cmd = self.codex.command(request(hands_on=True, writable=[Path("/外置硬盘"), Path("/另一块盘")]))
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")   # 不是"完全放开"
        self.assertNotIn("danger-full-access", cmd)
        self.assertEqual([cmd[i + 1] for i, part in enumerate(cmd) if part == "--add-dir"],
                         ["/外置硬盘", "/另一块盘"])
        self.assertEqual(cmd[cmd.index("-C") + 1], "/")                # 工作目录由调度器定
        self.assertEqual(cmd[cmd.index("-a") + 1], "never")            # 无人值守,不等人工审批

    def test_prompt_comes_before_images(self):
        cmd = Codex(Member("CODEX", "Codex", "codex", model="gpt")).command(
            Request(prompt="提示词", workdir=Path("/"), env={}, timeout=1, cancel=threading.Event(),
                    images=[Path("/图.png")]))
        self.assertLess(cmd.index("提示词"), cmd.index("-i"))           # -i 会吞掉它后面的参数
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt")


class CodexContext(unittest.TestCase):
    """Codex 的真实上下文长度要从它自己保存的会话记录里读。"""

    THREAD = "01a1e63f-b3ca-7b63-9735-7854f703d264"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = {"CODEX_HOME": str(self.home)}

    def record(self, steps, thread=THREAD):
        folder = self.home / "sessions" / "2026" / "11" / "16"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "session_meta", "payload": {"id": thread}},
                 {"type": "event_msg", "payload": {"type": "token_count", "info": None}}]
        total = 0
        for step in steps:
            total += step
            lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": total},
                "last_token_usage": {"input_tokens": step},
                "model_context_window": 475000}}})
        text = "\n".join(json.dumps(line) for line in lines) + "\n不是 JSON 的一行\n"
        (folder / f"rollout-2026-11-16T21-00-00-{thread}.jsonl").write_text(text, encoding="utf-8")

    def test_largest_single_step_not_the_running_total(self):
        self.record([20448, 20510, 19000])
        self.assertEqual(_codex_context(self.THREAD, self.env), (20510, 475000))

    def test_unknown_when_the_record_cannot_be_found(self):
        self.record([20448], thread="ffffffff-0000-0000-0000-000000000000")
        self.assertEqual(_codex_context(self.THREAD, self.env), (0, 0))
        self.assertEqual(_codex_context("", self.env), (0, 0))
        self.assertEqual(_codex_context("*", self.env), (0, 0))      # 不合法的编号不拿去找文件


if __name__ == "__main__":
    unittest.main()
