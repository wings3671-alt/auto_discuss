"""设置与路径:唯一知道"文件放在哪、成员有谁"的模块。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

USER_ID = "USER"
MEMBER_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
AI_KINDS = ("claude", "codex", "ollama", "command")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# ── 路径 ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Paths:
    """所有文件位置都从 root 推出。测试时换一个 root 即可与真实数据隔离。"""

    root: Path
    trash: Path = field(default_factory=lambda: Path.home() / ".Trash")

    @property
    def discussions(self) -> Path:
        return self.root / "讨论记录"

    @property
    def rules(self) -> Path:
        return self.discussions / "讨论守则.md"

    @property
    def data(self) -> Path:
        return self.root / "运行数据"

    @property
    def settings(self) -> Path:
        return self.data / "设置.json"

    @property
    def sync_log(self) -> Path:
        return self.data / "同步记录.json"

    @property
    def backups(self) -> Path:
        return self.data / "备份"

    @property
    def avatars(self) -> Path:
        return self.data / "头像"

    @property
    def log(self) -> Path:
        return self.data / "胡说八道群.log"

    def ensure(self) -> None:
        for folder in (self.discussions, self.data, self.backups, self.avatars):
            folder.mkdir(parents=True, exist_ok=True)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ── 取值小工具:类型不对就用默认值,绝不让错类型流进程序 ──────────────────────
def _str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


# ── 成员 ────────────────────────────────────────────────────────────────────
@dataclass
class Member:
    id: str                 # 写进讨论文档锚点的稳定编号,如 CLAUDE
    name: str               # 显示名,可随意改
    kind: str               # claude / codex / ollama / command / user
    avatar: str = ""        # 文字头像(一两个字或 emoji)
    color: str = "#888888"  # 头像底色
    image: str = ""         # 图片头像的文件名(在 头像/ 里);为空则用文字头像
    enabled: bool = True    # 新建讨论时是否入群
    model: str = ""         # 指定模型;ollama 必填,其余留空用默认
    command: str = ""       # kind=command 时的命令行

    @classmethod
    def from_dict(cls, raw: Any, fallback: "Member") -> "Member":
        raw = raw if isinstance(raw, dict) else {}
        color = _str(raw.get("color"), fallback.color)
        return cls(
            id=_str(raw.get("id"), fallback.id),
            name=_str(raw.get("name"), fallback.name).strip()[:24] or fallback.name,
            kind=_str(raw.get("kind"), fallback.kind),
            avatar=_str(raw.get("avatar"), fallback.avatar).strip()[:2],
            color=color if COLOR_RE.match(color) else fallback.color,
            image=Path(_str(raw.get("image"), fallback.image)).name,
            enabled=_bool(raw.get("enabled"), fallback.enabled),
            model=_str(raw.get("model"), fallback.model).strip(),
            command=_str(raw.get("command"), fallback.command).strip(),
        )


def default_user() -> Member:
    return Member(USER_ID, "我", "user", "我", "#3b5b8c")


def default_members() -> list[Member]:
    return [
        Member("CLAUDE", "Claude", "claude", "C", "#c9822b"),
        Member("CODEX", "Codex", "codex", "X", "#2f8f6b"),
    ]


_BLANK_AI = Member("", "新成员", "command", "", "#6b7fa8")


# ── 设置 ────────────────────────────────────────────────────────────────────
@dataclass
class Settings:
    port: int = 8765
    proxy: str = ""                         # AI 联网用的代理地址;为空 = 直连
    nas_sync: bool = False                  # 收工时把讨论复制到 nas_dir(只加不删)
    nas_mount: str = ""                     # NAS 的挂载点,用来判断它连没连上
    nas_dir: str = ""
    notify: bool = True
    max_rounds: int = 12
    turn_timeout: int = 600                 # 单次 AI 调用的超时秒数
    whiteboard_limit: int = 6000            # 白板超过这么多 token 就提示整理
    keep_backups: int = 10
    user: Member = field(default_factory=default_user)
    members: list[Member] = field(default_factory=default_members)

    @classmethod
    def from_dict(cls, raw: Any) -> "Settings":
        raw = raw if isinstance(raw, dict) else {}
        base = cls()
        user = Member.from_dict(raw.get("user"), base.user)
        user.id, user.kind = USER_ID, "user"
        return cls(
            port=_int(raw.get("port"), base.port, 1024, 65535),
            proxy=_str(raw.get("proxy"), base.proxy).strip(),
            nas_sync=_bool(raw.get("nas_sync"), base.nas_sync),
            nas_mount=_str(raw.get("nas_mount"), base.nas_mount).strip(),
            nas_dir=_str(raw.get("nas_dir"), base.nas_dir).strip(),
            notify=_bool(raw.get("notify"), base.notify),
            max_rounds=_int(raw.get("max_rounds"), base.max_rounds, 1, 200),
            turn_timeout=_int(raw.get("turn_timeout"), base.turn_timeout, 30, 3600),
            whiteboard_limit=_int(raw.get("whiteboard_limit"), base.whiteboard_limit, 500, 100000),
            keep_backups=_int(raw.get("keep_backups"), base.keep_backups, 1, 100),
            user=user,
            members=_valid_members(raw.get("members")) or base.members,
        )

    def member(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)

    def enabled_ids(self) -> list[str]:
        return [m.id for m in self.members if m.enabled]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _valid_members(raw: Any) -> list[Member]:
    """丢弃编号不合法、重复、类型未知的成员。"""
    members: list[Member] = []
    for item in raw if isinstance(raw, list) else []:
        member = Member.from_dict(item, _BLANK_AI)
        known = {m.id for m in members}
        if MEMBER_ID_RE.match(member.id) and member.id != USER_ID \
                and member.id not in known and member.kind in AI_KINDS:
            members.append(member)
    return members


def load_settings(path: Path) -> Settings:
    try:
        return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Settings()


def save_settings(path: Path, settings: Settings) -> None:
    write_json(path, settings.to_dict())


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, text: str) -> None:
    """原子写:先写临时文件再改名顶替,读的人只会看到完整的旧版或新版。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
