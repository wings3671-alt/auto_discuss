#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_discuss.py — 多方自动轮流讨论调度器(阶段 1a:三方轮流)
================================================================

作用
    让本机的 Claude Code、Codex,以及用户本人,围绕一份 Markdown 文档
    自动一轮一轮地交替发表意见,把一个议题讨论深、讨论透。

阶段 1a(本版本)
    一轮 = Claude → Codex → 用户。Claude、Codex 两棒自动连跑;到用户这一棒,
    程序写一个"占位段"到文档末尾,然后停下、轮询等待用户在文档里作答。
    用户那一轮 = 决策者指令;只有用户能终结讨论。
    加 --no-user 可退回旧的【两方全自动版】(Claude+Codex,规则 B)。

设计原则
    1. 只启动本机 CLI 的【非交互】任务,脚本本身不调用 OpenAI / Anthropic API。
       子进程里不继承 *_API_KEY,使 CLI 走本机已登录账号(订阅)。
    2. 脚本只做"调度器":判断轮到谁、调用谁、维护状态块。它不生成讨论内容。
    3. 永不删除 / 清空讨论文档。只让参与者在文末追加;状态块由本脚本独家维护。
    4. 每轮调用前自动【带时间戳】备份 + 加文件锁,防止互相覆盖、并保留多份回滚点。
    5. 等待用户期间只读讨论文档 + 定期刷新锁,绝不写讨论 Markdown。

用法
    python3 auto_discuss.py --file ~/Desktop/讨论记录/门头灯_0517.md
    python3 auto_discuss.py --file <md> --no-user      # 退回两方全自动
    python3 auto_discuss.py --file <md> --dry-run      # 只演示调度流程

状态块(脚本在文档标题正下方自动维护,缺失则自动创建)
    <!-- AUTO_DISCUSS
    enabled: true        是否启用自动讨论
    topic: 1             当前讨论的话题编号
    round: 1             当前轮次(一轮 = 三方各发言一次)
    turn: CLAUDE         下一步轮到谁:CLAUDE / CODEX / USER(三值枚举)
    status: WAITING      WAITING / RUNNING / WAITING_USER / DONE / ERROR
    max_rounds: 12       最大轮数(纯兜底)
    last_writer: NONE    上一次写入者
    last_updated: ...    上一次更新时间(UTC)
    stop: false          人工急停开关:改成 true 可让脚本下一轮安全退出
    pending_final: NONE  仅 --no-user 两方模式用:记录哪一方提了待确认的 FINAL_DONE
    -->

停止规则
    三方模式:只有用户在自己那轮写独立行『收工』才结束;或达 max_rounds;或报错。
              AI 写 FINAL_DONE 在 1a 里仅是给用户看的收尾建议,程序不响应。
    --no-user 两方模式:规则 B —— 双方连续两轮都写独立行 FINAL_DONE 即收敛。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

# ── CLI 可执行文件:优先用 PATH 里的,找不到再退回这两个已知绝对路径 ──────────
CLAUDE_FALLBACK = "/Users/wins/.local/bin/claude"
CODEX_FALLBACK = "/opt/homebrew/bin/codex"

# 一轮发言的正文若少于这么多字符,视为"低质量新增"
MIN_APPENDED_CHARS = 60

# 三方发言者:turn 关键字 → (emoji 圆点, 显示名)
SPEAKER = {
    "CLAUDE": ("🟡", "Claude Code"),
    "CODEX":  ("🟢", "Codex"),
    "USER":   ("🔴", "用户"),
}

# 状态块字段顺序(渲染时按此顺序输出)
STATUS_KEYS = ["enabled", "topic", "round", "turn", "status",
               "max_rounds", "last_writer", "last_updated", "stop",
               "pending_final"]

DEFAULT_STATUS = {
    "enabled": "true", "topic": "1", "round": "1", "turn": "CLAUDE",
    "status": "WAITING", "max_rounds": "12", "last_writer": "NONE",
    "last_updated": "", "stop": "false", "pending_final": "NONE",
}

# 匹配整个状态块(含首尾的 HTML 注释标记)
STATUS_RE = re.compile(r"<!--\s*AUTO_DISCUSS\s*(.*?)-->", re.DOTALL)

# 用户回合的机器锚点(4A.2):程序靠它定位"当前用户段"、支持断点续跑
USER_START_RE = re.compile(r"<!--\s*USER_TURN_START\s+round=(\d+)\s*-->")
USER_END_RE = re.compile(
    r"<!--\s*USER_TURN_END\s+round=(\d+)\s+status=(\w+)\s*-->")

# ── 用户占位段的固定文本(模板与解析共用同一份常量,避免不一致)─────────────
USER_DIVIDER = "──────────────  ✦  ──────────────"
USER_BANNER_TEXT = "老大,你的意见呢?"
USER_HINT_1 = "› 在这行下面写你的意见……"
USER_HINT_2 = "› 写完另起一行:  over = 继续下一轮   ·   收工 = 结束讨论"

# ── 讨论守则(批判性思维,第 5 节):每轮注入提示词;workdir 下的同名文件可覆盖 ──
RULES_FILENAME = "讨论守则.md"
DEFAULT_DISCUSS_RULES = (
    "## 讨论守则(每轮必读)\n"
    "1. 不要无条件附和其他参与者。对他们的观点做批判性审视,明确指出漏洞、"
    "风险、未被验证的假设。\n"
    "2. 拿出发散性思维:在别人方案之外,主动提出至少一个不同思路或反例。\n"
    "3. 讨论既要有深度(往下深挖细节),也要有广度(往外拓展被忽略的方面)。\n"
    "4. 宁可暴露分歧,也不要为了表面和气而含糊带过。\n"
)


# ── 基础工具 ────────────────────────────────────────────────────────────────
def now_iso():
    """当前时间,UTC,ISO 格式。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    """本地时间戳,用于备份文件名。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def log(log_path, msg):
    """同时打印到终端并追加到日志文件。"""
    line = f"[{now_iso()}] {msg}"
    print(line)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 日志写不进去也不应中断主流程


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path, text):
    """原子写:先写临时文件再 os.replace,避免中途崩溃留下半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── 状态块解析 / 渲染 ───────────────────────────────────────────────────────
def render_status(status):
    lines = ["<!-- AUTO_DISCUSS"]
    for k in STATUS_KEYS:
        lines.append(f"{k}: {status.get(k, '')}")
    lines.append("-->")
    return "\n".join(lines)


def _live_region(text):
    """返回"真状态块"应当所在的区域:第一个二级标题(## )之前。

    讨论文档正文里可能把 AUTO_DISCUSS 代码块作为示例引用,那段示例不能被
    误当成真状态块。真状态块永远插在文档标题正下方,即第一个 ## 之前。
    注:三方发言标题是 ###(h3),不含 "\\n## ",不会干扰本判定。"""
    idx = text.find("\n## ")
    return text if idx < 0 else text[:idx]


def parse_status(text):
    """从文档顶部区域解析真状态块;没有则返回 None。"""
    m = STATUS_RE.search(_live_region(text))
    if not m:
        return None
    status = dict(DEFAULT_STATUS)
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        status[k.strip()] = v.strip()
    return status


def ensure_status_block(text, max_rounds):
    """文档里没有状态块就插入一个(放在第一行标题之后)。返回 (新文本, status)。"""
    existing = parse_status(text)
    if existing:
        return text, existing
    status = dict(DEFAULT_STATUS)
    status["max_rounds"] = str(max_rounds)
    status["last_updated"] = now_iso()
    block = render_status(status)
    parts = text.split("\n", 1)
    if len(parts) == 2:                       # 插到标题行之后
        new_text = parts[0] + "\n\n" + block + "\n\n" + parts[1]
    else:
        new_text = block + "\n\n" + text
    return new_text, status


def write_status_into(text, status):
    """把文档顶部的真状态块整体替换为最新 status(只替换第一处)。"""
    return STATUS_RE.sub(lambda _m: render_status(status), text, count=1)


def body_of(text):
    """去掉状态块后的正文。脚本只往文末追加,所以正文是"只增不改"的。"""
    return STATUS_RE.sub("", text)


# ── 本轮新增内容的检测 ──────────────────────────────────────────────────────
def round_heading_re(turn, round_no):
    """匹配某一方某一轮的小节标题。

    ⚠️ 1a 起标题改为新格式 `### 🟡 Claude Code · 第 N 轮`,本正则已同步。
    为降低误判,容忍 emoji 缺失、间隔符为 · 或 -、空格差异。"""
    if turn == "CLAUDE":
        name = r"Claude\s*Code"
    elif turn == "CODEX":
        name = r"Codex"
    else:
        name = r"用户"
    return re.compile(
        rf"(?m)^#{{2,4}}\s*[🟡🟢🔴]?\s*{name}\s*[·\-]\s*第\s*{round_no}\s*轮")


FINAL_DONE_RE = re.compile(r"(?m)^\s*FINAL_DONE\s*$")


# ── 用户回合:占位段渲染 / 锚点定位 / 标记解析 / 意见提取 ─────────────────────
def render_user_placeholder(round_no):
    """生成用户占位段(含可见标题 + 不可见锚点)。"""
    dot, name = SPEAKER["USER"]
    return (
        f"<!-- USER_TURN_START round={round_no} -->\n"
        f"### {dot} {name} · 第 {round_no} 轮\n\n"
        f"{USER_DIVIDER}\n"
        f"       {USER_BANNER_TEXT}\n"
        f"{USER_DIVIDER}\n\n"
        f"{USER_HINT_1}\n\n\n\n"
        f"{USER_HINT_2}\n\n"
        f"<!-- USER_TURN_END round={round_no} status=waiting -->\n"
    )


def latest_user_section(text):
    """返回"最后一个 USER_TURN_START 之后到文末"的子串;没有则 None。

    4A.2:程序只解析最新用户段,旧轮里的 over/收工 永不会被误判。"""
    matches = list(USER_START_RE.finditer(text))
    if not matches:
        return None
    return text[matches[-1].start():]


def latest_user_round(text):
    """返回最新用户占位段的轮次号;没有则 None。用于断点续跑判定。"""
    matches = list(USER_START_RE.finditer(text))
    return int(matches[-1].group(1)) if matches else None


def mark_user_turn_done(text, round_no):
    """把指定轮次的 USER_TURN_END 锚点 status 由 waiting 改为 done。"""
    def repl(m):
        if int(m.group(1)) == round_no:
            return f"<!-- USER_TURN_END round={round_no} status=done -->"
        return m.group(0)
    return USER_END_RE.sub(repl, text)


# 标记行规整:strip 首尾空白,再去掉尾部标点(4A.3:对非技术用户从宽)
_TRAIL_PUNCT = " \t。.,,!!??;;、:·"


def _norm_marker_line(line):
    return line.strip().rstrip(_TRAIL_PUNCT).strip()


def find_user_marker(section):
    """在用户段内,按「独立整行」找 over / 收工。

    4A.3:over 大小写不敏感、容忍尾部标点;同段同时出现以最后一个为准。
    提示语行 USER_HINT_2 虽含 'over'/'收工' 字样,但整行规整后不等于标记,
    不会被误判(这正是"独立整行"要求的意义)。"""
    result = None
    for line in section.splitlines():
        n = _norm_marker_line(line)
        if n.lower() == "over":
            result = "over"
        elif n == "收工":
            result = "收工"
    return result


def extract_user_opinion(section):
    """从用户段提取用户真正写的意见。

    4A.4:剔除标题、横幅、提示语、锚点、控制标记 —— 那些是噪音,不传给 AI。"""
    keep = []
    for line in section.splitlines():
        s = line.strip()
        if not s:
            keep.append("")
            continue
        if s.startswith("<!--"):              # 锚点
            continue
        if s.startswith("#"):                 # 标题
            continue
        if s.startswith("›"):                 # 提示语
            continue
        if "✦" in s:                          # 横幅分隔线
            continue
        if s == USER_BANNER_TEXT:             # 横幅文字
            continue
        n = _norm_marker_line(line)
        if n.lower() == "over" or n == "收工":  # 控制标记
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def load_discuss_rules(workdir, log_path):
    """读取讨论守则:workdir 下有 讨论守则.md 就用它(用户可编辑),
    没有则创建一份默认守则并返回。每轮调用,以便用户中途改了能即时生效。"""
    path = os.path.join(workdir, RULES_FILENAME)
    if os.path.exists(path):
        try:
            return read(path)
        except OSError:
            return DEFAULT_DISCUSS_RULES
    try:
        write(path, DEFAULT_DISCUSS_RULES)
        log(log_path, f"未发现讨论守则,已创建默认 {RULES_FILENAME}(可自行编辑措辞)。")
    except OSError:
        pass
    return DEFAULT_DISCUSS_RULES


# ── 文件锁 ──────────────────────────────────────────────────────────────────
def acquire_lock(lock_path, timeout, file_path, log_path):
    """拿到锁返回 True;锁被占用且未过期返回 False。"""
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < timeout:
            log(log_path, f"锁文件存在且未过期({int(age)}s),可能有另一个调度在跑。退出。")
            return False
        try:
            log(log_path, f"发现过期锁({int(age)}s),内容:\n{read(lock_path)}")
        except OSError:
            pass
        os.remove(lock_path)
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()}\nstarted={now_iso()}\nfile={file_path}\n")
    return True


def touch_lock(lock_path):
    """刷新锁文件 mtime —— 等待用户期间唯一允许的写操作(4A.6)。"""
    try:
        os.utime(lock_path, None)
    except OSError:
        pass


def release_lock(lock_path):
    if os.path.exists(lock_path):
        os.remove(lock_path)


# ── 备份 ────────────────────────────────────────────────────────────────────
def make_backup(file_path, workdir, keep, log_path):
    """复制一份带时间戳的备份,并把旧备份裁剪到最多 keep 份。返回本次备份路径。"""
    backup_path = os.path.join(workdir, f"discuss.backup.{stamp()}.md")
    shutil.copy2(file_path, backup_path)
    log(log_path, f"已备份 → {backup_path}")
    backups = sorted(
        f for f in os.listdir(workdir)
        if re.fullmatch(r"discuss\.backup\.\d{8}-\d{6}\.md", f))
    for old in backups[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(workdir, old))
        except OSError:
            pass
    return backup_path


# ── 调用两个 CLI ────────────────────────────────────────────────────────────
def find_cli(name, fallback):
    return shutil.which(name) or fallback


def build_prompt(turn, round_no, topic, file_path, discuss_rules,
                 user_opinion=None, three_party=True):
    """生成给 CLI 的指令。脚本只让 AI 追加自己那一节,不许它碰别人的内容、
    状态块和用户锚点。三方模式下把用户上一轮发言列为"本轮必须执行项"。"""
    dot, name = SPEAKER[turn]
    heading = f"### {dot} {name} · 第 {round_no} 轮"
    lines = [
        f"你正在参与一份多方协作讨论文档。文档路径:{file_path}",
        f"请先用文件工具**完整读取**该文档,聚焦【话题 {topic}】的讨论内容。",
        f"当前轮到你(**{name}**)发言,这是第 {round_no} 轮。",
        "",
        discuss_rules.strip(),
        "",
        "严格要求:",
        f"1. 你**必须实际编辑** {file_path} 这个文件,在它的**末尾追加**一个"
        f"新小节。不允许只把意见输出到终端而不写文件。",
        f"2. 新小节标题严格写成独立一行:{heading}",
        f"   (标题前请保留 emoji 圆点 {dot},小节之间用一行 --- 分隔。)",
        f"3. 在该标题下写你这一轮的意见,**遵守上面的讨论守则**:对其他人"
        f"上一轮的观点做批判性回应,推动方案收敛,不要泛泛重复、不要无条件附和。",
        f"4. 绝对不要修改、删除或重写其他人已写过的任何内容,也不要改动文档"
        f"顶部 <!-- AUTO_DISCUSS ... --> 状态块、以及任何 <!-- USER_TURN_... --> "
        f"锚点。",
    ]
    if user_opinion and user_opinion.strip():
        lines += [
            "",
            "【本轮必须执行项 —— 来自项目决策者,不得忽略】",
            "用户是本项目的决策者,不是普通的「第三个意见」。用户在上一轮给出了"
            "下面的要求,你**必须逐条照办、不得敷衍、不得跳过**;若用户要求你去"
            "查某个网页或资料,你必须真的用联网工具去查,而不是凭印象作答。",
            "—— 用户上一轮原文(全文,不得压缩)——",
            user_opinion.strip(),
            "—— 用户原文结束 ——",
        ]
    if three_party:
        lines += [
            "",
            f"5. 关于结束:本讨论**只有用户能终结**。如果你认为讨论已经充分,"
            f"可以在你这一节末尾单独占一行写 FINAL_DONE,作为**给用户的收尾"
            f"建议**;但这只是建议,是否结束由用户决定,程序不会因它停止。",
        ]
    else:
        lines += [
            "",
            "5. 关于结束讨论(规则 B,双方共识才停):",
            "   - 只有当你认为讨论已充分、可给出最终可落地结论时,才在你这一节"
            "最后**单独占一行**写:FINAL_DONE",
            "   - 如果对方上一轮已写 FINAL_DONE、你也同意收尾,请在你这一轮同样"
            "单独一行写 FINAL_DONE 表示确认,讨论即结束。",
            "   - 如果你仍有实质分歧,**不要写 FINAL_DONE**,把意见写出来。",
        ]
    lines += [
        f"6. 写完后,你给终端的最终回复只需一句『已追加第 {round_no} 轮』即可,"
        f"不要把正文重复打印到终端。若无法写文件,请明确说明失败原因。",
    ]
    return "\n".join(lines)


def call_claude(prompt, workdir, timeout, log_path):
    """调用 Claude Code CLI 的非交互模式。"""
    claude = find_cli("claude", CLAUDE_FALLBACK)
    # 剔除 Claude Code 自身的会话环境变量(CLAUDE* / AI_AGENT):否则当本脚本
    # 是从一个 Claude Code 会话里启动时,被调起的 claude 会误以为自己嵌套在
    # 另一个会话里,直接空跑秒退、什么都不做。剔除后它作为全新顶层任务运行。
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") and k != "AI_AGENT"}
    # 不继承 API key:让 claude 走本机已登录账号(订阅);未登录则失败,不改走 API
    env.pop("ANTHROPIC_API_KEY", None)
    # 注意:不加 --add-dir。它是变长参数,会把后面的 prompt 一并吞掉,导致
    # claude 收不到提示词。工作目录(cwd)已是文档所在目录,无需 --add-dir。
    cmd = [
        claude, "-p",
        "--permission-mode", "acceptEdits",   # 自动批准文件编辑,实现无人值守
        prompt,
    ]
    log(log_path, f"调用 Claude Code CLI(cwd={workdir})…")
    return subprocess.run(cmd, cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=timeout)


def call_codex(prompt, workdir, timeout, log_path):
    """调用 Codex CLI 的非交互模式(codex exec)。"""
    codex = find_cli("codex", CODEX_FALLBACK)
    env = os.environ.copy()
    # 不继承 API key:让 codex 走本机已登录账号(ChatGPT 订阅);未登录则失败
    env.pop("OPENAI_API_KEY", None)
    cmd = [
        codex,
        "-a", "never",                   # 无人值守:从不停下来等人工审批
        "exec",
        "-C", workdir,                   # 工作根目录
        "--skip-git-repo-check",         # 桌面不是 git 仓库,跳过检查
        "-s", "workspace-write",         # 允许在工作目录内写文档
        prompt,
    ]
    log(log_path, f"调用 Codex CLI(cwd={workdir})…")
    return subprocess.run(cmd, cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=timeout)


# ── 用户回合:轮询等待 ──────────────────────────────────────────────────────
def wait_for_user(file_path, lock_path, round_no, interval, log_path):
    """无限期轮询,直到最新用户段出现独立整行的 over / 收工。
    返回 'over' 或 '收工'。

    等待期间(4A.6):只读讨论文档 + 定期 touch 锁文件,绝不写讨论 Markdown。"""
    log(log_path, f"⏳ 轮到你了(第 {round_no} 轮)。请在 VS Code 里打开文档,"
                  f"在占位段下写意见,另起一行打 over(继续)或 收工(结束),并保存。")
    warned_no_marker = False
    waited = 0
    while True:
        touch_lock(lock_path)             # 唯一允许的写操作:保持锁新鲜
        text = read(file_path)
        section = latest_user_section(text)
        if section is not None:
            marker = find_user_marker(section)
            if marker:
                log(log_path, f"检测到用户标记:『{marker}』。")
                return marker
            # 4A.3:已有实质内容但没标记 → 继续等待,并提示一次
            if extract_user_opinion(section) and not warned_no_marker:
                log(log_path, "已看到你的意见,但未检测到独立整行的 over / 收工,"
                              "继续等待…(写完请另起一行单独打 over 或 收工)")
                warned_no_marker = True
        time.sleep(interval)
        waited += interval
        if waited % 60 == 0:
            log(log_path, f"仍在等待用户回合…(已等 {waited // 60} 分钟)")


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="让 Claude Code、Codex 与用户围绕一份 md 自动轮流讨论的调度器")
    ap.add_argument("--file", required=True, help="讨论文档路径")
    ap.add_argument("--max-rounds", type=int, default=12,
                    help="最大轮数(纯兜底,默认 12)。一轮 = 三方各发言一次")
    ap.add_argument("--no-user", action="store_true",
                    help="退回旧的两方全自动模式(Claude+Codex,规则 B),不引入用户回合")
    ap.add_argument("--poll-interval", type=int, default=5,
                    help="等待用户时轮询文档的间隔秒数(默认 5)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="单次 CLI 调用超时秒数(默认 600)")
    ap.add_argument("--lock-timeout", type=int, default=1800,
                    help="锁文件超过该秒数即视为过期可清除(默认 1800)")
    ap.add_argument("--keep-backups", type=int, default=10,
                    help="最多保留多少份带时间戳的备份(默认 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只演示调度流程,不真正调用 CLI、不修改任何文件")
    args = ap.parse_args()

    file_path = os.path.abspath(args.file)
    if not os.path.exists(file_path):
        print(f"错误:讨论文档不存在 → {file_path}")
        sys.exit(1)

    workdir = os.path.dirname(file_path)
    lock_path = os.path.join(workdir, "discuss.lock")
    log_path = os.path.join(workdir, "auto_discuss.log")
    three_party = not args.no_user
    dry = args.dry_run

    log(log_path, "=" * 64)
    log(log_path, f"调度器启动  file={file_path}  max_rounds={args.max_rounds}  "
                  f"模式={'三方' if three_party else '两方(--no-user)'}  "
                  f"dry_run={dry}")

    if not dry and not acquire_lock(lock_path, args.lock_timeout, file_path, log_path):
        sys.exit(1)

    try:
        # 确保文档里有状态块(没有则创建)
        text0 = read(file_path)
        text_with_block, status = ensure_status_block(text0, args.max_rounds)
        if parse_status(text0) is None:
            if dry:
                log(log_path, "[dry-run] 文档无状态块,真实运行时会自动插入一个。")
            else:
                write(file_path, text_with_block)
                log(log_path, "文档中未发现状态块,已自动插入到标题正下方。")

        while True:
            # 真实模式每轮重新读盘:用户中途手动改 stop:true 也能被及时发现
            if not dry:
                disk_status = parse_status(read(file_path))
                if disk_status:
                    status = disk_status

            # —— 退出条件检查 ——
            if str(status.get("stop", "false")).lower() == "true":
                log(log_path, "状态块 stop=true,人工急停,退出。")
                break
            if str(status.get("enabled", "true")).lower() != "true":
                log(log_path, "状态块 enabled 非 true,退出。")
                break
            round_no = int(status.get("round", "1"))
            if round_no > args.max_rounds:
                log(log_path, f"已达最大轮数 {args.max_rounds},讨论结束,退出。")
                break

            turn = status.get("turn", "CLAUDE").upper()
            if turn not in ("CLAUDE", "CODEX", "USER"):
                turn = "CLAUDE"
            if turn == "USER" and not three_party:
                turn = "CLAUDE"          # --no-user 模式不应出现 USER,纠正
            topic = status.get("topic", "1")
            dot, writer_name = SPEAKER[turn]
            print(f"\n──── 第 {round_no} 轮 · 话题 {topic} · 轮到 "
                  f"{dot} {writer_name} ────")
            log(log_path, f"第 {round_no} 轮  turn={turn}  topic={topic}")

            final_done = False           # 仅 --no-user 模式用
            user_action = None           # 仅 USER 回合用

            # ════ AI 回合(CLAUDE / CODEX)════════════════════════════════
            if turn in ("CLAUDE", "CODEX"):
                if dry:
                    log(log_path, f"[dry-run] 此处本应调用 {writer_name},已跳过。")
                else:
                    # 取用户上一轮意见(三方模式),注入本轮 AI 提示词
                    user_opinion = None
                    if three_party:
                        sec = latest_user_section(read(file_path))
                        if sec:
                            user_opinion = extract_user_opinion(sec) or None
                    discuss_rules = load_discuss_rules(workdir, log_path)

                    backup_path = make_backup(file_path, workdir,
                                              args.keep_backups, log_path)
                    body_before = body_of(read(file_path))
                    status["status"] = "RUNNING"
                    write(file_path, write_status_into(read(file_path), status))

                    prompt = build_prompt(turn, round_no, topic, file_path,
                                          discuss_rules, user_opinion,
                                          three_party)
                    try:
                        if turn == "CLAUDE":
                            result = call_claude(prompt, workdir,
                                                 args.timeout, log_path)
                        else:
                            result = call_codex(prompt, workdir,
                                                args.timeout, log_path)
                    except subprocess.TimeoutExpired:
                        log(log_path, f"错误:{writer_name} 调用超时"
                                      f"(>{args.timeout}s),退出。")
                        status["status"] = "ERROR"
                        write(file_path,
                              write_status_into(read(file_path), status))
                        break

                    log(log_path, f"{writer_name} 退出码 = {result.returncode}")
                    if (result.stdout or "").strip():
                        log(log_path, f"{writer_name} stdout 末尾:\n"
                                      f"{(result.stdout or '')[-800:]}")
                    if (result.stderr or "").strip():
                        log(log_path, f"{writer_name} stderr 末尾:\n"
                                      f"{(result.stderr or '')[-800:]}")
                    if result.returncode != 0:
                        log(log_path, f"错误:{writer_name} 返回非 0,退出。")
                        status["status"] = "ERROR"
                        write(file_path,
                              write_status_into(read(file_path), status))
                        break

                    # 校验本轮产出
                    body_after = body_of(read(file_path))
                    if not body_after.startswith(body_before):
                        log(log_path, "严重:检测到已有正文被改写!"
                                      "从本轮备份恢复文件,退出。")
                        shutil.copy2(backup_path, file_path)
                        status = parse_status(read(file_path)) or status
                        status["status"] = "ERROR"
                        write(file_path,
                              write_status_into(read(file_path), status))
                        break
                    appended = body_after[len(body_before):]
                    if not round_heading_re(turn, round_no).search(appended):
                        log(log_path, f"错误:{writer_name} 没有写出本轮标题"
                                      f"「### {dot} {writer_name} · 第 "
                                      f"{round_no} 轮」,退出。")
                        status["status"] = "ERROR"
                        write(file_path,
                              write_status_into(read(file_path), status))
                        break
                    if len(appended.strip()) < MIN_APPENDED_CHARS:
                        log(log_path, f"错误:{writer_name} 本轮正文过短"
                                      f"({len(appended.strip())} 字符),退出。")
                        status["status"] = "ERROR"
                        write(file_path,
                              write_status_into(read(file_path), status))
                        break

                    # 规则 B:仅 --no-user 两方模式生效。三方模式 FINAL_DONE
                    # 只是给用户看的文字,程序不响应(1a 规格 4.5)。
                    if not three_party:
                        this_has_final = (
                            FINAL_DONE_RE.search(appended) is not None)
                        prev_pending = status.get(
                            "pending_final", "NONE").upper()
                        if this_has_final and prev_pending in (
                                "CLAUDE", "CODEX") and prev_pending != turn:
                            final_done = True
                            log(log_path, f"{writer_name} 确认 FINAL_DONE,"
                                          f"双方达成共识,讨论收敛。")
                        elif this_has_final:
                            status["pending_final"] = turn
                            log(log_path, f"{writer_name} 提出 FINAL_DONE,"
                                          f"等待对方下一轮确认。")
                        else:
                            status["pending_final"] = "NONE"

            # ════ 用户回合(USER,仅三方模式)═════════════════════════════
            else:
                if dry:
                    log(log_path, f"[dry-run] 此处会写第 {round_no} 轮用户占位段"
                                  f"并轮询等待;dry-run 直接当作 over 推进。")
                    user_action = "over"
                else:
                    cur = read(file_path)
                    if latest_user_round(cur) == round_no:
                        # 断点续跑:占位段已在(程序曾被杀),直接继续轮询
                        log(log_path, f"第 {round_no} 轮用户占位段已存在,"
                                      f"直接继续轮询(断点续跑)。")
                    else:
                        # 写占位段。状态块在"进入等待前"就更新好(4A.6)
                        make_backup(file_path, workdir,
                                    args.keep_backups, log_path)
                        status["turn"] = "USER"
                        status["status"] = "WAITING_USER"
                        status["last_updated"] = now_iso()
                        status["max_rounds"] = str(args.max_rounds)
                        new_text = (cur.rstrip() + "\n\n---\n\n"
                                    + render_user_placeholder(round_no))
                        new_text = write_status_into(new_text, status)
                        write(file_path, new_text)
                        log(log_path, f"已写入第 {round_no} 轮用户占位段。"
                                      f"它在 VS Code 里冒出来 = 轮到你了。")
                    # 轮询等待(无限期;期间只读 + touch 锁)
                    user_action = wait_for_user(
                        file_path, lock_path, round_no,
                        args.poll_interval, log_path)
                    # 用户回合结束:把 USER_TURN_END 锚点标为 done
                    write(file_path,
                          mark_user_turn_done(read(file_path), round_no))

            # —— 推进轮次:脚本是状态块的唯一维护者 ——
            status["last_writer"] = turn
            status["last_updated"] = now_iso()
            status["max_rounds"] = str(args.max_rounds)
            end_now = False
            if turn == "CLAUDE":
                status["turn"] = "CODEX"
            elif turn == "CODEX":
                if three_party:
                    status["turn"] = "USER"          # 三方:接下来轮到用户
                else:
                    status["turn"] = "CLAUDE"        # 两方:回到 Claude
                    status["round"] = str(round_no + 1)
            else:                                    # USER
                if user_action == "收工":
                    end_now = True                   # 只有用户能终结讨论
                else:                                # over
                    status["turn"] = "CLAUDE"
                    status["round"] = str(round_no + 1)
            if (not three_party) and final_done:
                end_now = True                       # 两方模式:规则 B 收敛

            status["status"] = "DONE" if end_now else "WAITING"
            if not dry:
                write(file_path,
                      write_status_into(read(file_path), status))
            log(log_path, f"状态推进 → round={status['round']}  "
                          f"turn={status['turn']}  status={status['status']}")

            if end_now:
                reason = ("用户写下『收工』" if turn == "USER"
                          else "双方 FINAL_DONE 共识")
                log(log_path, f"讨论结束({reason}),退出。")
                break

        log(log_path, "调度器正常结束。")
    except Exception as e:                          # noqa: BLE001
        log(log_path, f"未预期错误:{type(e).__name__}: {e}")
        raise
    finally:
        if not dry:
            release_lock(lock_path)
            log(log_path, "已释放文件锁。")


if __name__ == "__main__":
    main()
