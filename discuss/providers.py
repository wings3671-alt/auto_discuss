"""各家 AI 的调用方式。

每种成员类型对应一个 Provider:收到提示词,返回一段回复。
要接入新的模型,只需在这里加一个类并登记到 _KINDS。

计费铁律:只走各家命令行工具的订阅登录,子进程里不带任何 API key
(由 engine 在准备环境变量时统一剔除)。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Member

_CLAUDE_TALK_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch"      # 发言:只读文件 + 联网,不能写、不能跑命令
_CLAUDE_HANDS_ON_TOOLS = _CLAUDE_TALK_TOOLS + ",Edit,Write,Bash"   # 动手:再加上改文件、跑命令
_CODEX_THREAD_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
_LOGIN_WORDS = ("login", "log in", "401", "unauthor", "authenticat", "credential", "expired")
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S)


class ProviderError(Exception):
    """这一棒没成功。消息是写给用户看的中文。"""


class TurnCancelled(Exception):
    """用户点了停止。"""


def _ignore(_text: str) -> None:
    return None


@dataclass
class Request:
    prompt: str
    workdir: Path
    env: dict[str, str]
    timeout: int
    cancel: threading.Event
    images: list[Path] = field(default_factory=list)
    hands_on: bool = False                        # 这一棒被派了活:允许改文件、跑命令
    protected: list[Path] = field(default_factory=list)   # 动手时也不许写的文件夹
    on_text: Callable[[str], None] = _ignore      # 已输出的全文(不是增量)
    on_status: Callable[[str], None] = _ignore    # 此刻在做什么,如"正在联网搜索"


@dataclass
class Reply:
    text: str
    ctx: int = 0         # 上下文长度(token)
    out: int = 0         # 输出长度(token)
    window: int = 0      # 模型的上下文上限;未知为 0
    expires: str = ""    # 登录凭证的到期时间;未知为空


class Provider(Protocol):
    sees_images: bool
    assignable: bool     # 能不能被派活(动手改文件、跑命令);不能的只发言

    def run(self, request: Request) -> Reply: ...


# ── 公用:跑一个子进程,逐行读它的输出 ───────────────────────────────────────
def _run_process(cmd: list[str], request: Request, stdin_text: str,
                 on_line: Callable[[str], None], env: dict[str, str] | None = None
                 ) -> tuple[int, str]:
    """返回(退出码, 错误输出)。被用户停止抛 TurnCancelled,超时抛 ProviderError。"""
    try:
        proc = subprocess.Popen(
            cmd, cwd=request.workdir, env=env or request.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as err:
        raise ProviderError(f"启动不了命令 {cmd[0]}:{err}") from err

    errors: list[str] = []
    timed_out = threading.Event()
    deadline = time.monotonic() + request.timeout

    def feed() -> None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except OSError:
            pass

    def watch() -> None:
        while proc.poll() is None:
            if request.cancel.wait(0.2):
                return _kill(proc)
            if time.monotonic() > deadline:
                timed_out.set()
                return _kill(proc)

    helpers = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=watch, daemon=True),
               threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)]
    for helper in helpers:
        helper.start()
    try:
        for line in proc.stdout:
            on_line(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            _kill(proc)
        for helper in helpers:
            helper.join(timeout=5)

    if request.cancel.is_set():
        raise TurnCancelled()
    if timed_out.is_set():
        raise ProviderError(f"超过 {request.timeout} 秒没有完成,已中止。")
    return proc.returncode, "".join(errors)


def _kill(proc: subprocess.Popen) -> None:
    """结束整个进程组(AI 工具自己还会再起子进程)。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def _find(name: str, fallback: str, env: dict[str, str]) -> str:
    return shutil.which(name, path=env.get("PATH")) or os.path.expanduser(fallback)


def _json_line(line: str) -> dict[str, Any]:
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _num(data: Any, key: str) -> int:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _failure(who: str, code: int, stderr: str, login_hint: str) -> ProviderError:
    detail = stderr.strip()[-400:] or f"退出码 {code},没有给出原因"
    return ProviderError(_with_login_hint(f"{who} 没有正常完成:{detail}", login_hint))


def _with_login_hint(message: str, hint: str) -> str:
    lowered = message.lower()
    looks_like_login = any(word in lowered for word in _LOGIN_WORDS)
    return f"{message}\n{hint}" if hint and looks_like_login else message


# ── Claude Code ─────────────────────────────────────────────────────────────
class Claude:
    sees_images = True
    assignable = True
    _LOGIN_HINT = "看起来是登录问题:请打开终端运行 claude,输入 /login 重新登录后再点重试。"
    _TOOL_STATUS = {"Read": "正在查看文件", "Glob": "正在查找文件", "Grep": "正在检索内容",
                    "WebSearch": "正在联网搜索", "WebFetch": "正在打开网页",
                    "Edit": "正在修改文件", "Write": "正在写文件", "Bash": "正在执行命令"}

    def __init__(self, member: Member) -> None:
        self.model = member.model

    def command(self, request: Request) -> list[str]:
        """命令行。这一棒能做什么全在这里定:发言只给只读工具;被派活才加上改文件、跑命令。"""
        tools = _CLAUDE_HANDS_ON_TOOLS if request.hands_on else _CLAUDE_TALK_TOOLS
        cmd = [_find("claude", "~/.local/bin/claude", request.env), "-p",
               "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--strict-mcp-config",                    # 不加载用户的 MCP:省 token,也碰不到别的软件
               "--tools", tools, "--allowedTools", tools]
        if request.protected:                            # Edit(//绝对路径/**) 对所有改文件的工具都生效
            cmd += ["--disallowedTools", *[f"Edit(/{folder}/**)" for folder in request.protected]]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def run(self, request: Request) -> Reply:
        # 从 Claude Code 会话里启动时,要去掉会话变量,否则它以为自己被嵌套而空跑
        env = {key: value for key, value in request.env.items()
               if not key.startswith("CLAUDE") and key != "AI_AGENT"}

        live = ""
        final: dict[str, Any] = {}

        def on_line(line: str) -> None:
            nonlocal live, final
            event = _json_line(line)
            if event.get("type") == "result":
                final = event
                return
            inner = event.get("event") if event.get("type") == "stream_event" else None
            if not isinstance(inner, dict):
                return
            kind = inner.get("type")
            if kind == "message_start":
                live = ""
            elif kind == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool = str(block.get("name") or "")
                    request.on_status(self._TOOL_STATUS.get(tool, f"正在使用 {tool}"))
            elif kind == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    live += str(delta.get("text") or "")
                    request.on_text(live)

        code, stderr = _run_process(self.command(request), request, request.prompt, on_line, env)
        if not final:
            raise _failure("Claude", code, stderr, self._LOGIN_HINT)
        if final.get("is_error"):
            reason = str(final.get("result") or final.get("subtype") or "未知错误")
            raise ProviderError(_with_login_hint(f"Claude 报错:{reason}", self._LOGIN_HINT))
        usage = final.get("usage")
        return Reply(
            text=str(final.get("result") or ""),
            ctx=self._context_size(usage),
            out=_num(usage, "output_tokens"),
            window=max((_num(model, "contextWindow")
                        for model in (final.get("modelUsage") or {}).values()), default=0),
            expires=str(final.get("credential_expires_at") or ""))

    @staticmethod
    def _context_size(usage: Any) -> int:
        """一次调用内部可能有多步(查文件、搜索);上下文长度取最大的那一步。"""
        def size(step: Any) -> int:
            return (_num(step, "input_tokens") + _num(step, "cache_read_input_tokens")
                    + _num(step, "cache_creation_input_tokens"))
        steps = usage.get("iterations") if isinstance(usage, dict) else None
        return max(map(size, steps), default=0) if isinstance(steps, list) and steps \
            else size(usage)


# ── Codex ───────────────────────────────────────────────────────────────────
class Codex:
    sees_images = True
    assignable = False
    _LOGIN_HINT = "看起来是登录问题:请打开终端运行 codex login 重新登录后再点重试。"
    _ITEM_STATUS = {"reasoning": "正在思考", "command_execution": "正在查看文件",
                    "web_search": "正在联网搜索", "mcp_tool_call": "正在使用工具"}

    def __init__(self, member: Member) -> None:
        self.model = member.model

    def run(self, request: Request) -> Reply:
        cmd = [_find("codex", "/opt/homebrew/bin/codex", request.env),
               "-a", "never",                  # 无人值守:不停下来等人工审批
               "exec", "-C", str(request.workdir), "--skip-git-repo-check",
               "-s", "read-only",              # 只读沙箱:能看文件,不能写
               "--json"]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append(request.prompt)             # 提示词要放在 -i 之前,否则会被当成图片
        for image in request.images:
            cmd += ["-i", str(image)]

        text, problem, thread = "", "", ""
        usage: dict[str, Any] = {}

        def on_line(line: str) -> None:
            nonlocal text, problem, thread, usage
            event = _json_line(line)
            kind = event.get("type")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if kind == "thread.started":
                thread = str(event.get("thread_id") or "")
            elif kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                request.on_text(text)
            elif kind in ("item.started", "item.completed"):
                status = self._ITEM_STATUS.get(str(item.get("type")))
                if status:
                    request.on_status(status)
            elif kind == "turn.completed":
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            elif kind == "turn.failed":
                problem = str((event.get("error") or {}).get("message") or "未知错误")
            elif kind == "error":
                problem = str(event.get("message") or "未知错误")

        code, stderr = _run_process(cmd, request, "", on_line)
        if problem:
            raise ProviderError(_with_login_hint(f"Codex 报错:{problem}", self._LOGIN_HINT))
        if code != 0 or not text:
            raise _failure("Codex", code, stderr, self._LOGIN_HINT)
        ctx, window = _codex_context(thread, request.env)
        return Reply(text=text, ctx=ctx, out=_num(usage, "output_tokens"), window=window)


def _codex_context(thread: str, env: dict[str, str]) -> tuple[int, int]:
    """(一棒里最大那一步的上下文长度, 模型的上下文上限)。

    codex 的 --json 输出只给一棒内各步的累计值,比真实占用大得多;每一步的真实长度
    记在它自己保存的会话记录里,按会话编号去找。找不到就是 (0, 0):界面不显示这个数,
    不拿累计值充数。"""
    if not _CODEX_THREAD_RE.match(thread):
        return 0, 0
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    ctx = window = 0
    for record in home.glob(f"sessions/*/*/*/rollout-*-{thread}.jsonl"):
        try:
            lines = record.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            payload = _json_line(line).get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            step = info.get("last_token_usage") if isinstance(info, dict) else None
            ctx = max(ctx, _num(step, "input_tokens"))
            window = max(window, _num(info, "model_context_window"))
    return ctx, window


# ── 本地模型(Ollama)────────────────────────────────────────────────────────
class Ollama:
    sees_images = False
    assignable = False

    def __init__(self, member: Member) -> None:
        self.model = member.model

    def run(self, request: Request) -> Reply:
        if not self.model:
            raise ProviderError("这个本地模型成员还没填模型名(到 设置 → 成员 里填,如 qwen3:32b)。")
        body = json.dumps({"model": self.model, "stream": True,
                           "messages": [{"role": "user", "content": request.prompt}]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机服务不走代理
        try:
            response = opener.open(
                urllib.request.Request(_OLLAMA_URL, data=body.encode("utf-8"),
                                       headers={"Content-Type": "application/json"}),
                timeout=request.timeout)
        except urllib.error.HTTPError as err:
            raise ProviderError(f"Ollama 拒绝了请求({err.code}):请确认模型 {self.model} 已下载。") from err
        except OSError as err:
            raise ProviderError("连不上本机的 Ollama(127.0.0.1:11434),请确认它在运行。") from err

        deadline = time.monotonic() + request.timeout
        stopper = threading.Thread(
            target=lambda: request.cancel.wait(request.timeout) and response.close(), daemon=True)
        stopper.start()
        text, last = "", {}
        try:
            for raw in response:
                last = _json_line(raw.decode("utf-8", "replace"))
                text += str((last.get("message") or {}).get("content") or "")
                request.on_text(_THINK_RE.sub("", text).strip())
                if last.get("done") or time.monotonic() > deadline:
                    break
        except (OSError, ValueError) as err:
            if not request.cancel.is_set():
                raise ProviderError(f"本地模型中途断开:{err}") from err
        finally:
            response.close()
        if request.cancel.is_set():
            raise TurnCancelled()
        if not last.get("done"):
            raise ProviderError(f"超过 {request.timeout} 秒没有完成,已中止。")
        return Reply(text=_THINK_RE.sub("", text).strip(),
                     ctx=_num(last, "prompt_eval_count"), out=_num(last, "eval_count"))


# ── 任意命令行工具:提示词从标准输入进,回复从标准输出出 ───────────────────────
class Command:
    sees_images = False
    assignable = False

    def __init__(self, member: Member) -> None:
        self.command = member.command

    def run(self, request: Request) -> Reply:
        try:
            cmd = shlex.split(self.command)
        except ValueError as err:
            raise ProviderError(f"命令行写法有误:{err}") from err
        if not cmd:
            raise ProviderError("这个成员还没填命令行(到 设置 → 成员 里填)。")
        lines: list[str] = []

        def on_line(line: str) -> None:
            lines.append(line)
            request.on_text("".join(lines))

        code, stderr = _run_process(cmd, request, request.prompt, on_line)
        if code != 0:
            raise _failure(cmd[0], code, stderr, "")
        return Reply(text="".join(lines).strip())


_KINDS: dict[str, Callable[[Member], Provider]] = {
    "claude": Claude, "codex": Codex, "ollama": Ollama, "command": Command}


def create(member: Member) -> Provider:
    if member.kind not in _KINDS:
        raise ProviderError(f"不认识的成员类型:{member.kind}")
    return _KINDS[member.kind](member)
