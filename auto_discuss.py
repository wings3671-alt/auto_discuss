#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_discuss.py — Claude Code CLI 与 Codex CLI 自动轮流讨论调度器
================================================================

作用
    让本机的 Claude Code 与 Codex,围绕一份 Markdown 文档(默认 discuss.md)
    自动一轮一轮地交替发言,不需要人工每次去通知对方。

设计原则(对应需求文档"话题 2",并采纳了 Codex 第 3 轮的审阅意见)
    1. 只启动本机 CLI 的【非交互】任务,脚本本身不调用 OpenAI / Anthropic API。
       —— 子进程里不继承 *_API_KEY,使 CLI 走本机已登录账号(订阅);
          若未登录则任务失败,而不会自动改走 API。
    2. 脚本只做"调度器":判断轮到谁、调用谁、维护状态块。它不生成讨论内容。
    3. 永不删除 / 清空 discuss.md。只让 CLI 在文末追加;状态块由本脚本独家维护。
    4. 每轮调用前自动【带时间戳】备份 + 加文件锁,防止互相覆盖、并保留多份回滚点。
    5. 出错、达最大轮数、检测到本轮新增区域里的 FINAL_DONE、或 stop:true 时退出。

用法
    python3 auto_discuss.py --file /Users/wins/Desktop/discuss.md --max-rounds 6
    python3 auto_discuss.py --file /Users/wins/Desktop/discuss.md --dry-run
        ↑ dry-run:只演示调度流程,不真正调用 CLI、不修改任何文件。

状态块(脚本在文档标题正下方自动维护,缺失则自动创建)
    <!-- AUTO_DISCUSS
    enabled: true        是否启用自动讨论
    topic: 1             当前讨论的话题编号
    round: 1             当前轮次(一轮 = Claude + Codex 各发言一次)
    turn: CLAUDE         下一步轮到谁:CLAUDE 或 CODEX
    status: WAITING      WAITING / RUNNING / DONE / ERROR
    max_rounds: 6        最大轮数
    last_writer: NONE    上一次写入者
    last_updated: ...    上一次更新时间(UTC)
    stop: false          人工急停开关:改成 true 可让脚本下一轮安全退出
    pending_final: NONE  规则 B 用:记录哪一方提了待确认的 FINAL_DONE
    -->

停止规则(规则 B,双方共识才停)
    讨论在以下任一情况结束,谁先到就停:
    - 双方在连续两轮里都写了独立一行的 FINAL_DONE(达成共识);
    - 轮次超过 max_rounds(兜底,防止聊不拢时无限循环);
    - 状态块 stop:true、enabled 非 true、CLI 报错、本轮产出不合格。
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

# 状态块字段顺序(渲染时按此顺序输出)
STATUS_KEYS = ["enabled", "topic", "round", "turn", "status",
               "max_rounds", "last_writer", "last_updated", "stop",
               "pending_final"]

DEFAULT_STATUS = {
    "enabled": "true", "topic": "1", "round": "1", "turn": "CLAUDE",
    "status": "WAITING", "max_rounds": "6", "last_writer": "NONE",
    "last_updated": "", "stop": "false", "pending_final": "NONE",
}

# 匹配整个状态块(含首尾的 HTML 注释标记)
STATUS_RE = re.compile(r"<!--\s*AUTO_DISCUSS\s*(.*?)-->", re.DOTALL)


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

    这样做是因为讨论文档正文里可能把 AUTO_DISCUSS 代码块作为"示例"引用
    (本文档话题 2 的需求说明里就有),那段示例不能被误当成真状态块。
    真状态块永远插在文档标题正下方,即第一个 ## 之前。"""
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


# ── 本轮新增内容的检测(采纳 Codex 意见 #4 #5)──────────────────────────────
def round_heading_re(turn, round_no):
    """匹配某一方某一轮的小节标题,容忍空格差异。"""
    name = "Claude Code" if turn == "CLAUDE" else "Codex"
    return re.compile(
        rf"(?m)^#{{2,4}}\s*{re.escape(name)}\s*-\s*第\s*{round_no}\s*轮")


FINAL_DONE_RE = re.compile(r"(?m)^\s*FINAL_DONE\s*$")


# ── 文件锁 ──────────────────────────────────────────────────────────────────
def acquire_lock(lock_path, timeout, file_path, log_path):
    """拿到锁返回 True;锁被占用且未过期返回 False。"""
    if os.path.exists(lock_path):
        age = time.time() - os.path.getmtime(lock_path)
        if age < timeout:
            log(log_path, f"锁文件存在且未过期({int(age)}s),可能有另一个调度在跑。退出。")
            return False
        # 过期锁:先把内容记进日志再清除(Codex 意见 #6,便于排查)
        try:
            log(log_path, f"发现过期锁({int(age)}s),内容:\n{read(lock_path)}")
        except OSError:
            pass
        os.remove(lock_path)
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()}\nstarted={now_iso()}\nfile={file_path}\n")
    return True


def release_lock(lock_path):
    if os.path.exists(lock_path):
        os.remove(lock_path)


# ── 备份(采纳 Codex 意见 #7:带时间戳,保留多份)─────────────────────────────
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


def build_prompt(turn, round_no, topic, file_path):
    """生成给 CLI 的指令。脚本只让 AI 追加自己那一节,不许它碰别人的内容和状态块。"""
    name = "Claude Code" if turn == "CLAUDE" else "Codex"
    return (
        f"你正在参与一份多方协作讨论文档。文档路径:{file_path}\n"
        f"请先用文件工具**完整读取**该文档,聚焦【话题 {topic}】的讨论内容。\n"
        f"当前轮到你(**{name}**)发言,这是第 {round_no} 轮。\n\n"
        f"严格要求:\n"
        f"1. 你**必须实际编辑** {file_path} 这个文件,在它的**末尾追加**一个新小节。"
        f"不允许只把意见输出到终端而不写文件。\n"
        f"2. 新小节标题严格写成独立一行:### {name} - 第 {round_no} 轮\n"
        f"3. 在该标题下写你这一轮的意见:针对对方上一轮提出的问题与分歧做出回应,"
        f"推动方案收敛,不要泛泛重复。\n"
        f"4. 绝对不要修改、删除或重写其他人已经写过的任何内容,也不要改动文档顶部"
        f" <!-- AUTO_DISCUSS ... --> 状态块(它由调度脚本维护)。\n"
        f"5. 关于结束讨论(规则 B,双方共识才停):\n"
        f"   - 只有当你认为讨论已经充分、可以给出最终可落地结论时,才在你这一节"
        f"的最后**单独占一行**写:FINAL_DONE\n"
        f"   - 如果对方上一轮已经写了 FINAL_DONE、而你也同意收尾,请在你这一轮"
        f"同样单独一行写 FINAL_DONE 表示确认,讨论即结束。\n"
        f"   - 如果你仍有实质性意见或分歧,**不要写 FINAL_DONE**,把意见写出来,"
        f"讨论继续。\n"
        f"6. 写完后,你给终端的最终回复只需一句『已追加第 {round_no} 轮』即可,"
        f"不要把正文重复打印到终端。若无法写文件,请明确说明失败原因。\n"
    )


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
    # 注意:不加 --add-dir。它是变长参数(可接多个目录),会把后面的 prompt
    # 一并吞掉,导致 claude 收不到提示词。工作目录(cwd)已是 discuss.md 所在
    # 目录,claude 默认即可读写,无需 --add-dir。
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
        "-a", "never",                   # 无人值守:从不停下来等人工审批(Codex 意见 #1)
        "exec",
        "-C", workdir,                   # 工作根目录
        "--skip-git-repo-check",         # 桌面不是 git 仓库,跳过检查
        "-s", "workspace-write",         # 允许在工作目录内写 discuss.md
        prompt,
    ]
    log(log_path, f"调用 Codex CLI(cwd={workdir})…")
    return subprocess.run(cmd, cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=timeout)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="让 Claude Code 与 Codex 围绕 discuss.md 自动轮流讨论的调度器")
    ap.add_argument("--file", required=True, help="讨论文档路径")
    ap.add_argument("--max-rounds", type=int, default=6,
                    help="最大轮数,一轮 = Claude + Codex 各发言一次(默认 6)")
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

    log(log_path, "=" * 64)
    log(log_path, f"调度器启动  file={file_path}  max_rounds={args.max_rounds}  "
                  f"dry_run={args.dry_run}")

    dry = args.dry_run
    if not dry and not acquire_lock(lock_path, args.lock_timeout, file_path, log_path):
        sys.exit(1)

    try:
        # 确保文档里有状态块(没有则创建)
        text0 = read(file_path)
        text_with_block, status = ensure_status_block(text0, args.max_rounds)
        if parse_status(text0) is None:   # 用顶部区域判断,避免被正文示例块干扰
            if dry:
                log(log_path, "[dry-run] 文档无状态块,真实运行时会自动插入一个。")
            else:
                write(file_path, text_with_block)
                log(log_path, "文档中未发现状态块,已自动插入到标题正下方。")

        while True:
            # 真实模式下每轮重新读盘:这样用户中途手动改 stop:true 也能被及时发现
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
            # --max-rounds 始终是权威轮数上限,覆盖状态块里可能残留的旧值
            if round_no > args.max_rounds:
                log(log_path, f"已达最大轮数 {args.max_rounds},讨论结束,退出。")
                break

            turn = status.get("turn", "CLAUDE").upper()
            topic = status.get("topic", "1")
            writer_name = "Claude Code" if turn == "CLAUDE" else "Codex"
            print(f"\n──── 第 {round_no} 轮 · 话题 {topic} · 轮到 {writer_name} ────")
            log(log_path, f"第 {round_no} 轮  turn={turn}  topic={topic}")

            final_done = False

            if dry:
                log(log_path, f"[dry-run] 此处本应调用 {writer_name},已跳过。")
            else:
                # 1) 带时间戳备份(本轮的回滚点)
                backup_path = make_backup(file_path, workdir, args.keep_backups, log_path)
                # 2) 记录调用前正文(脚本只追加,所以正文是只增不改的)
                body_before = body_of(read(file_path))
                # 3) 把状态标记为 RUNNING
                status["status"] = "RUNNING"
                write(file_path, write_status_into(read(file_path), status))
                # 4) 调用对应 CLI
                prompt = build_prompt(turn, round_no, topic, file_path)
                try:
                    if turn == "CLAUDE":
                        result = call_claude(prompt, workdir, args.timeout, log_path)
                    else:
                        result = call_codex(prompt, workdir, args.timeout, log_path)
                except subprocess.TimeoutExpired:
                    log(log_path, f"错误:{writer_name} 调用超时(>{args.timeout}s),退出。")
                    status["status"] = "ERROR"
                    write(file_path, write_status_into(read(file_path), status))
                    break
                # 5) 记录 CLI 输出尾部,方便排查
                log(log_path, f"{writer_name} 退出码 = {result.returncode}")
                if (result.stdout or "").strip():
                    log(log_path, f"{writer_name} stdout 末尾:\n"
                                  f"{(result.stdout or '')[-800:]}")
                if (result.stderr or "").strip():
                    log(log_path, f"{writer_name} stderr 末尾:\n"
                                  f"{(result.stderr or '')[-800:]}")
                # 6) 非 0 退出码 → 报错停止
                if result.returncode != 0:
                    log(log_path, f"错误:{writer_name} 返回非 0,退出。")
                    status["status"] = "ERROR"
                    write(file_path, write_status_into(read(file_path), status))
                    break

                # 7) 校验本轮产出 ——
                body_after = body_of(read(file_path))
                # 7a) 前文不能被改动:正文必须是"在 body_before 之后追加"
                if not body_after.startswith(body_before):
                    log(log_path, "严重:检测到已有正文被改写!从本轮备份恢复文件,退出。")
                    shutil.copy2(backup_path, file_path)
                    status = parse_status(read(file_path)) or status
                    status["status"] = "ERROR"
                    write(file_path, write_status_into(read(file_path), status))
                    break
                appended = body_after[len(body_before):]
                # 7b) 必须出现本轮标题
                if not round_heading_re(turn, round_no).search(appended):
                    log(log_path, f"错误:{writer_name} 没有写出本轮标题"
                                  f"「### {writer_name} - 第 {round_no} 轮」,退出。")
                    status["status"] = "ERROR"
                    write(file_path, write_status_into(read(file_path), status))
                    break
                # 7c) 正文不能过短
                if len(appended.strip()) < MIN_APPENDED_CHARS:
                    log(log_path, f"错误:{writer_name} 本轮正文过短"
                                  f"({len(appended.strip())} 字符),疑似无效,退出。")
                    status["status"] = "ERROR"
                    write(file_path, write_status_into(read(file_path), status))
                    break
                # 7d) 规则 B:双方共识才停。只在"本轮新增区域"里按独立整行检测
                #     FINAL_DONE(文档别处的 FINAL_DONE 字样不会误触发);必须双方
                #     连续两轮都写 FINAL_DONE,才算讨论收敛。
                this_has_final = FINAL_DONE_RE.search(appended) is not None
                prev_pending = status.get("pending_final", "NONE").upper()
                if this_has_final and prev_pending in ("CLAUDE", "CODEX") \
                        and prev_pending != turn:
                    final_done = True   # 对方上轮已 FINAL_DONE,本轮也确认 → 双方共识
                    log(log_path, f"{writer_name} 确认 FINAL_DONE,双方达成共识,讨论收敛。")
                elif this_has_final:
                    status["pending_final"] = turn   # 本方先提,待对方下一轮确认
                    log(log_path, f"{writer_name} 提出 FINAL_DONE,等待对方下一轮确认。")
                else:
                    status["pending_final"] = "NONE"  # 本方未确认 → 讨论继续

            # —— 推进轮次:脚本是状态块的唯一维护者 ——
            status["last_writer"] = turn
            status["last_updated"] = now_iso()
            status["max_rounds"] = str(args.max_rounds)   # 让状态块显示与本次一致
            if turn == "CLAUDE":
                status["turn"] = "CODEX"            # Claude 说完 → 轮到 Codex
            else:
                status["turn"] = "CLAUDE"           # Codex 说完 → 回到 Claude
                status["round"] = str(round_no + 1) # 一整轮结束,轮次 +1
            status["status"] = "DONE" if final_done else "WAITING"
            if not dry:
                write(file_path, write_status_into(read(file_path), status))
            log(log_path, f"状态推进 → round={status['round']}  turn={status['turn']}"
                          f"  status={status['status']}")

            if final_done:
                log(log_path, "检测到 FINAL_DONE,讨论已收敛,退出。")
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
