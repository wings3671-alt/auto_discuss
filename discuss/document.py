"""讨论文档(Markdown)的读与写。

文档既是存档也是唯一的数据来源,格式与旧的终端版完全兼容:

    # 标题
    <!-- AUTO_DISCUSS ... -->          状态块:轮到谁、第几轮、这一回派活给谁
    ## 话题 1
    议题正文
    ---
    <!-- TURN round=1 speaker=CLAUDE time=... ctx=... out=... -->
    ### 🟡 Claude · 第 1 轮
    AI 发言(锚点里带 hands_on=1 的,是被派活、有动手权限的那一棒)
    > 📋 白板更新摘要
    ---
    <!-- USER_TURN_START round=1 v=2 time=... -->
    ### 🔴 用户 · 第 1 轮
    用户发言
    <!-- USER_TURN_END round=1 status=done -->

读:parse() 把整篇解析成 Document,只读不改。
写:只做定点修改(换状态块、文末追加、改标题或议题),从不整体重排,
    所以旧讨论里的任何文字都不会被改动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LEGACY_MEMBERS = ["CLAUDE", "CODEX"]   # 旧文档没写成员名单时的默认值
USER_ID = "USER"


class Phase:
    NEW = "NEW"                # 还没写议题
    ACTIVE = "ACTIVE"          # 轮到某个 AI(是否正在跑由调度器掌握)
    USER = "WAITING_USER"      # 轮到用户
    DONE = "DONE"              # 已收工


_LEGACY_PHASE = {"WAITING": Phase.ACTIVE, "RUNNING": Phase.ACTIVE, "ERROR": Phase.ACTIVE}
_PHASES = {Phase.NEW, Phase.ACTIVE, Phase.USER, Phase.DONE}

_STATUS_RE = re.compile(r"<!--\s*AUTO_DISCUSS\s*\n(.*?)-->", re.S)
_ANCHOR_RE = re.compile(
    r"^<!--[ \t]*(TURN|USER_TURN_START|USER_TURN_END|NOTE)\b([^>\n]*)-->[ \t]*$", re.M)
_ATTR_RE = re.compile(r"(\w+)=(\S+)")
_HEADING_RE = re.compile(r"^#{2,4}\s*(?:[^\w\s]+\s+)?(.+?)\s*[·\-]\s*第\s*\d+\s*轮\s*$")
_TRAILING_RULE_RE = re.compile(r"(?:\A|\n)\s*-{3,}\s*\Z")     # 段落末尾的分隔线
_RESERVED_LINE_RE = re.compile(
    r"^<!--(?=\s*(?:TURN|USER_TURN_START|USER_TURN_END|NOTE|AUTO_DISCUSS)\b)", re.M)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

_DOTS = {"CLAUDE": "🟡", "CODEX": "🟢", USER_ID: "🔴"}
_TOPIC_HINT = "在下面写下你想讨论的问题"
_MARKER_PUNCT = " \t。.,,!!??;;、:·"
_LEGACY_BANNER = "老大,你的意见呢?"


# ── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class Status:
    phase: str = Phase.NEW
    round: int = 1
    turn: str = ""                      # 下一个发言者的编号;用户为 USER
    max_rounds: int = 12
    members: list[str] = field(default_factory=list)
    updated: str = ""
    assignee: str = ""                  # 这一回被派活的成员编号;只讨论、没派活时为空

    def render(self) -> str:
        return "\n".join([
            "<!-- AUTO_DISCUSS",
            f"status: {self.phase}",
            f"round: {self.round}",
            f"turn: {self.turn}",
            f"max_rounds: {self.max_rounds}",
            f"members: {','.join(self.members)}",
            *([f"assignee: {self.assignee}"] if self.assignee else []),
            f"last_updated: {self.updated}",
            "-->",
        ])


@dataclass
class Message:
    kind: str            # topic / ai / user / note
    speaker: str         # 成员编号;note 为空
    round: int
    text: str
    name: str = ""       # 写入时的显示名(成员后来被删掉时用它兜底)
    time: str = ""
    ctx: int = 0         # 这次调用的上下文长度(token)
    out: int = 0         # 输出长度(token)
    note: str = ""       # 白板更新摘要
    hands_on: bool = False   # 这条 AI 发言是被派活的那一棒:有动手权限,不只是发表意见


@dataclass
class Document:
    title: str
    status: Status | None
    topic: str
    messages: list[Message]
    draft: str = ""      # 旧版占位段里用户已写但未提交的文字


# ── 读 ──────────────────────────────────────────────────────────────────────
def parse(text: str) -> Document:
    anchors = list(_ANCHOR_RE.finditer(text))
    intro = text[:anchors[0].start()] if anchors else text
    topic = _topic_of(intro)
    messages = [Message("topic", USER_ID, 0, topic)] if topic else []
    status = _parse_status(text)
    unclosed: Message | None = None      # 最后一个没标记"已完成"的用户段

    index = 0
    while index < len(anchors):
        anchor = anchors[index]
        kind, attrs = anchor.group(1), _attrs(anchor.group(2))
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        body = text[anchor.end():end]
        round_no = _to_int(attrs.get("round"))

        if kind == "TURN":
            messages.append(_ai_message(attrs, round_no, body))
        elif kind == "NOTE":
            messages.append(Message("note", "", round_no, _strip_quote(body),
                                    time=attrs.get("time", "")))
        elif kind == "USER_TURN_START":
            closing: dict[str, str] | None = None
            if index + 1 < len(anchors) and anchors[index + 1].group(1) == "USER_TURN_END":
                closing = _attrs(anchors[index + 1].group(2))
                index += 1
            message = Message("user", USER_ID, round_no,
                              _user_text(body, legacy=attrs.get("v") != "2"),
                              time=attrs.get("time", ""))
            messages.append(message)
            unclosed = message if closing is None or closing.get("status") == "waiting" else None
        index += 1

    # 文档正等着用户、且最后一段还没提交:那一段是草稿,不算发言。
    # (更早的旧版从不标记"已完成",所以其余未标记的段落都是真发言。)
    draft = ""
    if unclosed is not None and messages[-1] is unclosed \
            and (status is None or status.phase == Phase.USER):
        draft = messages.pop().text
    return Document(_title_of(text), status, topic, messages, draft)


def _attrs(raw: str) -> dict[str, str]:
    return dict(_ATTR_RE.findall(raw))


def _to_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _title_of(text: str) -> str:
    first = text.split("\n", 1)[0].strip()
    return first[2:].strip() if first.startswith("# ") else first


def _status_match(text: str) -> re.Match[str] | None:
    """真状态块只可能在第一个二级标题之前;正文里引用的示例不算。"""
    cut = text.find("\n## ")
    return _STATUS_RE.search(text if cut < 0 else text[:cut])


def _parse_status(text: str) -> Status | None:
    match = _status_match(text)
    if not match:
        return None
    raw: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            raw[key.strip()] = value.strip()
    phase = raw.get("status", Phase.NEW).upper()
    phase = _LEGACY_PHASE.get(phase, phase)
    members = [m for m in raw.get("members", "").split(",") if m] if "members" in raw \
        else list(LEGACY_MEMBERS)
    return Status(
        phase=phase if phase in _PHASES else Phase.ACTIVE,
        round=max(1, _to_int(raw.get("round"), 1)),
        turn=raw.get("turn", "").upper(),
        max_rounds=max(1, _to_int(raw.get("max_rounds"), 12)),
        members=members,
        updated=raw.get("last_updated", ""),
        assignee=raw.get("assignee", "").upper(),
    )


def _topic_of(intro: str) -> str:
    match = _status_match(intro)
    if match:
        intro = intro[:match.start()] + intro[match.end():]
    lines = intro.split("\n")[1:]                       # 去掉标题行
    kept = [line for line in lines
            if not line.startswith("## 话题") and _TOPIC_HINT not in line]
    return _TRAILING_RULE_RE.sub("", "\n".join(kept).strip()).strip()


def _ai_message(attrs: dict[str, str], round_no: int, body: str) -> Message:
    lines = _TRAILING_RULE_RE.sub("", body.strip()).strip().split("\n")
    name = ""
    heading = _HEADING_RE.match(lines[0]) if lines else None
    if heading:
        name = heading.group(1)
        lines = lines[1:]
    note = ""
    if lines and lines[-1].startswith(("> 📋", "> ⚠")):
        note = lines.pop()[2:].replace("详见 STATE.md。", "").strip()
    return Message(
        "ai", attrs.get("speaker", "").upper(), round_no, "\n".join(lines).strip(),
        name=name, time=attrs.get("time", ""),
        ctx=_to_int(attrs.get("ctx")), out=_to_int(attrs.get("out")), note=note,
        hands_on=attrs.get("hands_on") == "1")


def _strip_quote(body: str) -> str:
    body = _TRAILING_RULE_RE.sub("", body.strip()).strip()
    return "\n".join(line[1:].strip() if line.startswith(">") else line
                     for line in body.split("\n")).strip()


def _user_text(body: str, legacy: bool) -> str:
    """取出用户真正写的话。旧版占位段里的横幅、提示语、over/收工 标记都是噪音。"""
    kept: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#") and _HEADING_RE.match(stripped):
            continue
        if legacy:
            marker = stripped.rstrip(_MARKER_PUNCT).strip().lower()
            if (stripped.startswith("›") or "✦" in stripped
                    or stripped == _LEGACY_BANNER or marker in ("over", "收工")):
                continue
        kept.append(line)
    return _TRAILING_RULE_RE.sub("", "\n".join(kept).strip()).strip()


def image_paths(text: str) -> list[str]:
    """发言里用 ![名称](路径) 引用的图片路径。"""
    return IMAGE_RE.findall(text)


# ── 写 ──────────────────────────────────────────────────────────────────────
def new_document(title: str, status: Status) -> str:
    return f"# {title}\n\n{status.render()}\n\n## 话题 1\n\n"


def with_status(text: str, status: Status) -> str:
    match = _status_match(text)
    if match:
        return text[:match.start()] + status.render() + text[match.end():]
    head, _, rest = text.partition("\n")
    return f"{head}\n\n{status.render()}\n\n{rest.lstrip(chr(10))}"


def with_title(text: str, title: str) -> str:
    head, sep, rest = text.partition("\n")
    return f"# {title}{sep}{rest}" if head.startswith("# ") else f"# {title}\n\n{text}"


def with_topic(text: str, topic: str) -> str:
    """把议题写到「## 话题」标题下面(只在还没有任何发言时使用)。"""
    match = _ANCHOR_RE.search(text)
    intro, rest = (text[:match.start()], text[match.start():]) if match else (text, "")
    heading = re.search(r"^## 话题.*$", intro, re.M)
    head = intro[:heading.end()] if heading else intro.rstrip("\n") + "\n\n## 话题 1"
    tail = f"\n---\n\n{rest}" if rest else ""
    return f"{head}\n\n{_safe(topic)}\n{tail}"


def with_ai_message(text: str, speaker: str, name: str, round_no: int, body: str,
                    note: str, time: str, ctx: int, out: int, hands_on: bool = False) -> str:
    block = (f"<!-- TURN round={round_no} speaker={speaker} time={time} "
             f"ctx={ctx} out={out}{' hands_on=1' if hands_on else ''} -->\n"
             f"### {dot_of(speaker)} {name} · 第 {round_no} 轮\n\n"
             f"{_safe(body)}\n\n> {note}")
    return _append(text, block)


def with_user_message(text: str, round_no: int, body: str, time: str,
                      into_placeholder: bool = False) -> str:
    """写入用户发言,追加到文末。

    into_placeholder:文档正等着用户发言时,旧版会在文末留一个占位段;
    为真则把发言写进那个占位段,而不是在它后面再追加一段。"""
    block = (f"<!-- USER_TURN_START round={round_no} v=2 time={time} -->\n"
             f"### {dot_of(USER_ID)} 用户 · 第 {round_no} 轮\n\n"
             f"{_safe(body)}\n\n"
             f"<!-- USER_TURN_END round={round_no} status=done -->")
    span = _pending_span(text) if into_placeholder else None
    if span:
        return text[:span[0]] + block + "\n" + text[span[1]:].lstrip("\n")
    return _append(text, block)


def with_note(text: str, round_no: int, note: str, time: str) -> str:
    return _append(text, f"<!-- NOTE round={round_no} time={time} -->\n> {note}")


def dot_of(speaker: str) -> str:
    return _DOTS.get(speaker, "🔵")


def _append(text: str, block: str) -> str:
    return text.rstrip("\n") + "\n\n---\n\n" + block + "\n"


def _safe(body: str) -> str:
    """正文里若出现与程序锚点同形的行,转义掉,免得下次解析时被当成真锚点。"""
    return _RESERVED_LINE_RE.sub("&lt;!--", body.strip())


def _pending_span(text: str) -> tuple[int, int] | None:
    """最后一个仍在等待的用户占位段(旧版写的)在文中的起止位置。"""
    anchors = list(_ANCHOR_RE.finditer(text))
    for index in range(len(anchors) - 1, -1, -1):
        if anchors[index].group(1) != "USER_TURN_START":
            continue
        following = anchors[index + 1] if index + 1 < len(anchors) else None
        if following is None:
            return anchors[index].start(), len(text)
        if following.group(1) == "USER_TURN_END" \
                and _attrs(following.group(2)).get("status") == "waiting":
            return anchors[index].start(), following.end()
        return None
    return None
