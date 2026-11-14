"""状态白板(STATE.md)与 STATE_OPS 指令。

白板是整场讨论的滚动摘要,AI 每轮靠它了解前情,所以不必重读全文。
AI 不直接改白板,只在回复末尾输出一段 STATE_OPS 指令(JSON),
由程序校验后执行。便签编号只增不复用,免得 AI 引用到被回收的旧号。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# (机器名, 编号前缀, 显示标题)
SECTIONS: list[tuple[str, str, str]] = [
    ("conclusions", "C", "已达成的结论"),
    ("open_questions", "Q", "待解决的问题"),
    ("rejected", "R", "已否决的方案"),
    ("constraints", "K", "关键约束 / 前提"),
]
_PREFIX = {key: prefix for key, prefix, _ in SECTIONS}
_KEY_BY_TITLE = {title: key for key, _, title in SECTIONS}
_SHORT = {"conclusions": "结论", "open_questions": "待解决问题",
          "rejected": "否决方案", "constraints": "约束"}

_META_RE = re.compile(
    r"<!--\s*STATE_META\s+next_C=(\d+)\s+next_Q=(\d+)\s+next_R=(\d+)\s+next_K=(\d+)\s*-->")
_ITEM_RE = re.compile(
    r"^-\s*\[([CQRK]\d+)\]\s*(.*?)\s*(?:<!--\s*touched:\s*(\S+)\s*-->)?\s*$")
_ID_RE = re.compile(r"^[CQRK]\d+$")
_OPS_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*STATE_OPS[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.I | re.M | re.S)
_OPS_START_RE = re.compile(r"^[ \t]*```[ \t]*STATE_OPS", re.I | re.M)
_CJK_RE = re.compile(r"[⺀-鿿豈-﫿＀-￯]")

Op = dict[str, Any]


@dataclass
class Item:
    id: str
    text: str
    touched: str      # 最后改动它的人,如 r2-claude


@dataclass
class Board:
    counters: dict[str, int] = field(
        default_factory=lambda: {prefix: 1 for _, prefix, _ in SECTIONS})
    sections: dict[str, list[Item]] = field(
        default_factory=lambda: {key: [] for key, _, _ in SECTIONS})

    def ids(self) -> set[str]:
        return {item.id for items in self.sections.values() for item in items}

    def is_empty(self) -> bool:
        return not self.ids()


class BoardError(ValueError):
    """白板文件损坏,或 STATE_OPS 不合格。"""


# ── 白板文件 ────────────────────────────────────────────────────────────────
def render(board: Board) -> str:
    c = board.counters
    lines = [
        f"<!-- STATE_META next_C={c['C']} next_Q={c['Q']} next_R={c['R']} next_K={c['K']} -->",
        "# 讨论滚动状态",
        "",
        "> 本文件由程序独家维护,请勿手动编辑。要修正,请在发言里说明,由下一轮 AI 改。",
        "",
    ]
    for key, _, title in SECTIONS:
        lines.append(f"## {title}")
        lines += [f"- [{item.id}] {item.text}   <!-- touched: {item.touched} -->"
                  for item in board.sections[key]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse(text: str) -> Board:
    meta = _META_RE.search(text)
    if not meta:
        raise BoardError("白板文件缺少 STATE_META 行")
    board = Board(counters=dict(zip("CQRK", map(int, meta.groups()))))
    current: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            current = _KEY_BY_TITLE.get(line[3:].strip())
        elif current and line.startswith("- ["):
            item = _ITEM_RE.match(line)
            if item:
                board.sections[current].append(
                    Item(item.group(1), item.group(2).strip(), item.group(3) or "?"))
    return board


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数:中文约一字一个,其余约四个字符一个。"""
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 4


# ── STATE_OPS ───────────────────────────────────────────────────────────────
def split_reply(reply: str) -> tuple[str, list[str]]:
    """把 AI 回复拆成(发言正文, STATE_OPS 代码块列表)。"""
    blocks = _OPS_BLOCK_RE.findall(reply)
    return _OPS_BLOCK_RE.sub("", reply).strip(), blocks


def visible_part(partial_reply: str) -> str:
    """AI 还在输出时给用户看的部分:STATE_OPS 一开始就截掉,不让机器指令露出来。"""
    start = _OPS_START_RE.search(partial_reply)
    return (partial_reply[:start.start()] if start else partial_reply).strip()


def read_ops(reply: str, board: Board) -> list[Op]:
    """取出并校验回复里的 STATE_OPS。不合格则抛 BoardError,说明原因。"""
    _, blocks = split_reply(reply)
    if len(blocks) != 1:
        raise BoardError(f"找到 {len(blocks)} 个 STATE_OPS 代码块(应恰好 1 个)")
    try:
        payload = json.loads(blocks[0])
    except ValueError as err:
        raise BoardError(f"STATE_OPS 不是合法 JSON({err})") from err
    return validate(payload, board)


def validate(payload: Any, board: Board) -> list[Op]:
    if not isinstance(payload, dict) or set(payload) != {"ops"}:
        raise BoardError("顶层必须是只含 ops 一个键的 JSON 对象")
    ops = payload["ops"]
    if not isinstance(ops, list):
        raise BoardError("ops 必须是数组")
    existing, used = board.ids(), set()
    for number, op in enumerate(ops, 1):
        where = f"第 {number} 条"
        if not isinstance(op, dict):
            raise BoardError(f"{where}不是对象")
        kind = op.get("op")
        if kind == "add":
            _need_keys(op, {"op", "section", "text"}, where)
            if op["section"] not in _PREFIX:
                raise BoardError(f"{where}的 section 非法:{op['section']}")
            _need_text(op, where)
        elif kind in ("update", "remove"):
            _need_keys(op, {"op", "id", "text"} if kind == "update" else {"op", "id"}, where)
            item_id = op["id"]
            if not isinstance(item_id, str) or not _ID_RE.match(item_id):
                raise BoardError(f"{where}的 id 格式非法:{item_id}")
            if item_id not in existing:
                raise BoardError(f"{where}的 id 在白板上不存在:{item_id}")
            if item_id in used:
                raise BoardError(f"编号 {item_id} 在同一批里被引用多次")
            used.add(item_id)
            if kind == "update":
                _need_text(op, where)
        else:
            raise BoardError(f"{where}的 op 非法:{kind}")
    return ops


def _need_keys(op: Op, keys: set[str], where: str) -> None:
    if set(op) != keys:
        raise BoardError(f"{where}的字段应恰为 {sorted(keys)}")


def _need_text(op: Op, where: str) -> None:
    if not isinstance(op["text"], str) or not op["text"].strip():
        raise BoardError(f"{where}的 text 不能为空")


def apply(board: Board, ops: list[Op], touched: str) -> None:
    """执行已校验的指令(原地修改 board)。"""
    for op in ops:
        if op["op"] == "add":
            prefix = _PREFIX[op["section"]]
            board.sections[op["section"]].append(
                Item(f"{prefix}{board.counters[prefix]}", op["text"].strip(), touched))
            board.counters[prefix] += 1
            continue
        for key, items in board.sections.items():
            if op["op"] == "remove":
                board.sections[key] = [item for item in items if item.id != op["id"]]
            else:
                for item in items:
                    if item.id == op["id"]:
                        item.text, item.touched = op["text"].strip(), touched


def summarize(ops: list[Op]) -> str:
    """把一批指令概括成一句人话,写在发言末尾。"""
    if not ops:
        return "白板无变化"
    added: dict[str, int] = {}
    for op in ops:
        if op["op"] == "add":
            added[op["section"]] = added.get(op["section"], 0) + 1
    parts = [f"新增 {count} 条{_SHORT[section]}" for section, count in added.items()]
    for verb, kind in (("修订", "update"), ("删除", "remove")):
        ids = [op["id"] for op in ops if op["op"] == kind]
        if ids:
            parts.append(f"{verb} {'、'.join(ids)}")
    return "白板:" + ";".join(parts)
