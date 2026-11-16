"""调度:轮到谁、调用谁、把什么写进文档。

程序是讨论文档与白板的唯一执笔人。AI 平时只输出文字,碰不到任何文件;
用户可以在某一回把活派给一位成员(assignee),只有它、只在那一棒有动手权限
(hands_on),干完把执行报告当作发言交回来。讨论文档与白板它仍然不许改。
每场讨论同一时间只有一个后台任务(一棒发言,或一次白板整理)。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from . import document, prompts, providers, whiteboard
from .config import (AI_KINDS, USER_ID, Member, Paths, Settings, load_settings,
                     save_settings, write_json, write_text)
from .document import Document, Phase, Status
from .providers import Provider, ProviderError, Reply, Request, TurnCancelled

log = logging.getLogger("discuss")

_EXTEND_BY = 6                      # 到达轮数上限后,一次延长几轮
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_MAX_AVATAR_BYTES = 15 * 1024 * 1024
_IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}
_PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
               "/usr/bin", "/bin"]
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\s\x00-\x1f]+')
_UNSAFE_ATTACHMENT_NAME_RE = re.compile(r"[^\w\-]+")
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")
_PHASE_VIEW = {Phase.NEW: "new", Phase.ACTIVE: "paused", Phase.USER: "user", Phase.DONE: "done"}


class EngineError(Exception):
    """用户的操作做不了。消息是写给用户看的中文;code 是对应的 HTTP 状态码。"""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── 事件广播:界面靠它知道"有变化了" ─────────────────────────────────────────
class Hub:
    def __init__(self) -> None:
        self._listeners: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        listener: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._lock:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass                      # 界面太久没读:丢掉,它重连后会整体刷新


# ── 一场讨论的运行时状态(不落盘)────────────────────────────────────────────
@dataclass
class Live:
    """正在发言的那一棒:谁、已经说了什么、此刻在做什么。"""

    speaker: str
    text: str = ""
    status: str = ""
    started: float = 0.0


class Session:
    def __init__(self, sid: str, path: Path) -> None:
        self.id = sid
        self.path = path
        self.state_path = path.with_name(f"{sid}.state.md")
        self.attachments = path.with_name(f"{sid}_附件")
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.busy = False
        self.job = ""                     # turn(发言)/ tidy(整理白板)
        self.live: Live | None = None
        self.error = ""                   # 上一棒失败的原因,给用户看


@dataclass
class Attached:
    """用户附给 AI 看的东西,都在这场讨论的附件夹里。"""

    images: list[Path]            # 最近一次发言附的图片
    files: list[Path]             # 最近一次发言附的文件
    earlier_files: list[Path]     # 更早的发言里附过的文件


def _reveal_in_finder(path: Path) -> None:
    subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)


class Engine:
    def __init__(self, paths: Paths,
                 make_provider: Callable[[Member], Provider] = providers.create,
                 reveal: Callable[[Path], None] = _reveal_in_finder) -> None:
        paths.ensure()
        self.paths = paths
        self._reveal = reveal
        self.settings: Settings = load_settings(paths.settings)
        self.hub = Hub()
        self._make_provider = make_provider
        self._sessions: dict[str, Session] = {}
        self._registry_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._health: dict[str, dict[str, Any]] = {}
        self._last_live_event = 0.0

    # ── 查询 ────────────────────────────────────────────────────────────────
    def summaries(self) -> list[dict[str, Any]]:
        """讨论列表:置顶的在前,其余按最近改动排序。"""
        pinned = self._pins()
        rows = []
        for session, doc in self._documents():
            status = self._status(doc)
            rows.append({
                "id": session.id, "title": doc.title or session.id,
                "phase": self._phase(session, status), "round": status.round,
                "turn": self._name(status.turn), "hands_on": status.turn == status.assignee,
                "pinned": session.id in pinned,
                "modified": session.path.stat().st_mtime})
        return sorted(rows, key=lambda row: (row["pinned"], row["modified"]), reverse=True)

    def search(self, query: str) -> list[dict[str, str]]:
        """在标题和所有发言里找 query(不分大小写)。每场讨论给出第一处命中的上下文;
        命中在标题里时摘要为空。"""
        needle = " ".join(query.lower().split())
        hits = []
        for session, doc in self._documents() if needle else ():
            if needle in doc.title.lower():
                hits.append({"id": session.id, "snippet": ""})
                continue
            for message in doc.messages:
                text = " ".join(message.text.split())
                position = text.lower().find(needle)
                if position >= 0:
                    start = max(0, position - 10)
                    excerpt = text[start:position + len(needle) + 30]
                    hits.append({"id": session.id, "snippet": ("…" if start else "") + excerpt})
                    break
        return hits

    def view(self, sid: str) -> dict[str, Any]:
        session = self._session(sid)
        doc = self._read(session)
        status = self._status(doc)
        board, board_problem = self._board_or_blank(session)
        speakers = {m.speaker: m.name for m in doc.messages if m.kind == "ai"}
        people = {USER_ID: asdict(self.settings.user)}
        for member_id in dict.fromkeys([*status.members, *speakers]):
            people[member_id] = self._person(member_id, speakers.get(member_id, ""))
        return {
            "id": sid, "title": doc.title or sid,
            "phase": self._phase(session, status),
            "round": status.round, "max_rounds": status.max_rounds, "turn": status.turn,
            "assignee": status.assignee,
            "members": status.members, "people": people,
            "messages": [asdict(message) for message in doc.messages],
            "draft": doc.draft,
            "board": {
                "sections": [{"key": key, "title": title,
                              "items": [asdict(item) for item in board.sections[key]]}
                             for key, _, title in whiteboard.SECTIONS],
                "tokens": whiteboard.estimate_tokens(whiteboard.render(board)),
                "limit": self.settings.whiteboard_limit, "problem": board_problem},
            "job": session.job, "error": session.error,
            "live": asdict(session.live) if session.live else None,
            "health": {member_id: self._health[member_id]
                       for member_id in status.members if member_id in self._health},
            "sync": self._sync_state(sid),
            "attachments": session.attachments.name,
        }

    def settings_view(self) -> dict[str, Any]:
        return {**self.settings.to_dict(), "rules": self._rules(), "kinds": list(AI_KINDS),
                "proxy_alive": _reachable(self.settings.proxy),
                "nas_online": bool(self.settings.nas_mount)
                and os.path.ismount(self.settings.nas_mount)}

    def attachment(self, sid: str, name: str) -> Path:
        """界面要显示的附图。只给图片:别的附件不经网页送出,免得里面的网页脚本在界面里运行。"""
        path = _existing_child(self._session(sid).attachments, name)
        if path.suffix[1:] not in _IMAGE_TYPES.values():
            raise EngineError("找不到这个文件。", 404)
        return path

    def avatar(self, name: str) -> Path:
        return _existing_child(self.paths.avatars, name)

    # ── 用户的操作 ──────────────────────────────────────────────────────────
    def create(self, title: str) -> str:
        title = " ".join(title.split())[:60] or "未命名讨论"
        members = self.settings.enabled_ids()
        if not members:
            raise EngineError("至少要启用一个 AI 成员(设置 → 成员)。")
        stem = (_UNSAFE_NAME_RE.sub("_", title).strip("._")[:40] or "讨论") \
            + datetime.now().strftime("_%m%d")
        if (self.paths.discussions / f"{stem}.md").exists():
            stem += datetime.now().strftime("-%H%M%S")
        status = Status(max_rounds=self.settings.max_rounds, members=members, updated=_now())
        write_text(self.paths.discussions / f"{stem}.md", document.new_document(title, status))
        self.hub.publish({"type": "list"})
        return stem

    def send(self, sid: str, text: str, finish: bool = False, extend: bool = False,
             interrupt: bool = False, assignee: str = "") -> dict[str, Any]:
        """用户发言。新讨论里它是议题;已收工的讨论里它接着往下聊;AI 出错暂停时它是插话。

        interrupt:AI 正在发言时,先打断它,再把这段话作为插话交给它重说这一棒。
        assignee:这一回把活派给哪位成员(只有它那一棒能动手);为空 = 这一回只讨论。"""
        session = self._session(sid)
        text = text.strip()
        if interrupt and text and session.busy and session.job == "turn":
            self._interrupt(session)
        with session.lock:
            self._require_idle(session)
            raw = session.path.read_text(encoding="utf-8")
            doc = document.parse(raw)
            status = self._status(doc)
            first = status.members[0] if status.members else ""
            waiting = status.phase == Phase.USER          # 旧版此时在文末留有占位段
            if not first:
                raise EngineError("这场讨论没有 AI 成员(设置 → 成员)。")
            if assignee and not finish:
                self._check_assignee(status, assignee)

            if status.phase == Phase.NEW:
                if finish or not (text or doc.topic):
                    raise EngineError("先写下你想讨论的问题。")
                if text:
                    raw = document.with_topic(raw, text)
                status = replace(status, phase=Phase.ACTIVE, round=1, turn=first,
                                 assignee=assignee)
            elif finish:
                if text or waiting:
                    raw = document.with_user_message(raw, status.round, text or "收工。",
                                                     _now(), into_placeholder=waiting)
                status = replace(status, phase=Phase.DONE, turn=USER_ID, assignee="")
            elif not text:
                raise EngineError("写点什么再发送。")
            elif status.phase == Phase.ACTIVE:              # 暂停中的插话:不换轮,原地继续
                raw = document.with_user_message(raw, status.round, text, _now())
                status = replace(status, assignee=assignee or status.assignee)
            else:                                           # 轮到用户,或重新打开已收工的讨论
                upcoming = status.round + 1
                if upcoming > status.max_rounds:
                    if not extend:
                        return {"need_extend": True, "max_rounds": status.max_rounds,
                                "extend_by": _EXTEND_BY}
                    status = replace(status, max_rounds=status.max_rounds + _EXTEND_BY)
                target = upcoming if status.phase == Phase.DONE else status.round
                raw = document.with_user_message(raw, target, text, _now(),
                                                 into_placeholder=waiting)
                status = replace(status, phase=Phase.ACTIVE, round=upcoming, turn=first,
                                 assignee=assignee)

            self._backup(session)
            status = replace(status, updated=_now())
            write_text(session.path, document.with_status(raw, status))
            session.error = ""

        if status.phase == Phase.DONE:
            self.sync_later(sid)
        self._continue(session, status)
        return {"ok": True}

    def resume(self, sid: str) -> None:
        session = self._session(sid)
        with session.lock:
            self._require_idle(session)
            status = self._status(self._read(session))
            if status.phase != Phase.ACTIVE:
                raise EngineError("现在没有等着发言的 AI。")
        self._continue(session, status)

    def skip(self, sid: str) -> None:
        session = self._session(sid)
        with session.lock:
            self._require_idle(session)
            raw = session.path.read_text(encoding="utf-8")
            status = self._status(document.parse(raw))
            if status.phase != Phase.ACTIVE:
                raise EngineError("现在没有可以跳过的发言。")
            note = f"⏭ 跳过了 {self._name(status.turn)} 的第 {status.round} 轮发言。"
            if status.turn == status.assignee:              # 被派活的那位被跳过:这一回没人干活了
                status = replace(status, assignee="")
            status = _after(status)
            write_text(session.path, document.with_status(
                document.with_note(raw, status.round, note, _now()), status))
            session.error = ""
        self._continue(session, status)

    def stop(self, sid: str) -> None:
        self._session(sid).cancel.set()

    def _interrupt(self, session: Session) -> None:
        """叫停正在发言的 AI,并等它真的停下来。"""
        session.cancel.set()
        deadline = time.monotonic() + 15
        while session.busy:
            if time.monotonic() > deadline:
                raise EngineError("没能停下正在发言的 AI,请稍后再试。", 409)
            time.sleep(0.05)

    def pin(self, sid: str, pinned: bool) -> None:
        self._session(sid)
        others = [item for item in self._pins() if item != sid]
        write_json(self.paths.pins, [sid, *others] if pinned else others)
        self.hub.publish({"type": "list"})

    def tidy(self, sid: str) -> None:
        session = self._session(sid)
        if self._board(session).is_empty():
            raise EngineError("白板还是空的,不需要整理。")
        self._start(session, "tidy", self._tidy)

    def rename(self, sid: str, title: str) -> None:
        title = " ".join(title.split())[:60]
        if not title:
            raise EngineError("标题不能为空。")
        session = self._session(sid)
        with session.lock:
            raw = session.path.read_text(encoding="utf-8")
            write_text(session.path, document.with_title(raw, title))
        self._changed(session)

    def trash(self, sid: str, with_nas: bool = False) -> dict[str, str]:
        """把一场讨论连同白板、附件、备份移进废纸篓(可在废纸篓里找回)。

        with_nas:NAS 上的备份也一起处理——挪进 NAS 的「已删除_日期」文件夹,不直接删。
        返回的 nas 字段说明 NAS 那一份的去向:kept / moved / none / offline / failed。"""
        session = self._session(sid)
        with session.lock:
            self._require_idle(session)
            self.paths.trash.mkdir(parents=True, exist_ok=True)
            moves = [(session.path, session.path.name),
                     (session.state_path, session.state_path.name),
                     (session.attachments, session.attachments.name),
                     (self.paths.backups / sid, f"{sid}_备份")]
            for source, name in moves:
                if source.exists():
                    shutil.move(str(source), str(_free_name(self.paths.trash / name)))
        with self._registry_lock:
            self._sessions.pop(sid, None)
        with self._sync_lock:
            records = self._sync_records()
            if records.pop(sid, None):
                write_json(self.paths.sync_log, records)
        if sid in self._pins():
            write_json(self.paths.pins, [item for item in self._pins() if item != sid])
        outcome = self._retire_nas_copy(session) if with_nas else "kept"
        self.hub.publish({"type": "list"})
        return {"nas": outcome}

    def _retire_nas_copy(self, session: Session) -> str:
        folder = self._nas_folder()
        if folder is None:
            return "offline"
        copies = [folder / path.name
                  for path in (session.path, session.state_path, session.attachments)]
        copies = [copy for copy in copies if copy.exists()]
        if not copies:
            return "none"
        try:
            retired = folder / f"已删除_{datetime.now():%Y-%m-%d}"
            retired.mkdir(exist_ok=True)
            for copy in copies:
                shutil.move(str(copy), str(_free_name(retired / copy.name)))
        except OSError as err:
            log.warning("挪动 NAS 上的备份失败:%s", err)
            return "failed"
        return "moved"

    def save_export(self, sid: str, html: bytes) -> str:
        """把界面生成的导出网页存进「下载」文件夹,并在访达里显示它。返回文件路径。"""
        title = self._read(self._session(sid)).title
        name = _UNSAFE_NAME_RE.sub("_", title).strip("._")[:60] or sid
        self.paths.downloads.mkdir(parents=True, exist_ok=True)
        target = _free_name(self.paths.downloads / f"{name}.html")
        target.write_bytes(html)
        self._reveal(target)
        return str(target)

    def attach(self, sid: str, filename: str, data: bytes) -> dict[str, Any]:
        """保存用户附的一份文件,返回可以插进发言的 Markdown。

        PNG / JPG 算图片:界面里直接显示,AI 当图看。其余算文件:界面里是一枚文件签,
        AI 用读文件工具打开。存下来的文件名只留字母、数字、汉字,不带空格和括号。"""
        session = self._session(sid)
        if not data:
            raise EngineError("这是个空文件。")
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise EngineError("文件太大了(上限 50 MB)。", 413)
        original = Path(filename)
        image = _image_extension(data)
        extension = f".{image}" if image else original.suffix.lower()
        stem = _UNSAFE_ATTACHMENT_NAME_RE.sub("_", original.stem).strip("_")[:30] \
            or ("图片" if image else "文件")
        label = stem if image else stem + (extension if _EXTENSION_RE.fullmatch(extension) else "")
        session.attachments.mkdir(exist_ok=True)
        target = _free_name(session.attachments / f"{datetime.now():%H%M%S}_{label}"
                            f"{extension if image else ''}")
        target.write_bytes(data)
        link = f"[{label}]({session.attachments.name}/{target.name})"
        return {"name": target.name, "label": label, "image": bool(image),
                "markdown": f"!{link}" if image else link}

    def reveal(self, sid: str, name: str) -> None:
        """在访达里显示这场讨论的一份附件。"""
        self._reveal(_existing_child(self._session(sid).attachments, name))

    def set_avatar(self, member_id: str, data: bytes) -> str:
        member = self._member_or_user(member_id)
        extension = _image_extension(data)
        if not extension or len(data) > _MAX_AVATAR_BYTES:
            raise EngineError("头像只接受 PNG 和 JPG 图片,最大 15 MB。", 415)
        name = f"{member_id}_{datetime.now():%Y%m%d%H%M%S}.{extension}"
        (self.paths.avatars / name).write_bytes(data)
        member.image = name
        save_settings(self.paths.settings, self.settings)
        self.hub.publish({"type": "settings"})
        return name

    def update_settings(self, raw: Any) -> None:
        updated = Settings.from_dict(raw)
        if not updated.enabled_ids():
            raise EngineError("至少要启用一个 AI 成员。")
        for member in updated.members:
            if member.kind == "ollama" and not member.model:
                raise EngineError(f"本地模型成员「{member.name}」需要填模型名。")
            if member.kind == "command" and not member.command:
                raise EngineError(f"命令行成员「{member.name}」需要填命令行。")
        rules = raw.get("rules") if isinstance(raw, dict) else None
        if isinstance(rules, str) and rules.strip() and rules.strip() != self._rules().strip():
            write_text(self.paths.rules, rules.strip() + "\n")
        save_settings(self.paths.settings, updated)
        self.settings = updated
        self.hub.publish({"type": "settings"})
        self.hub.publish({"type": "list"})

    def shutdown(self) -> None:
        """退出前:叫停所有正在发言的 AI。"""
        with self._registry_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.cancel.set()
        deadline = time.monotonic() + 8
        while any(session.busy for session in sessions) and time.monotonic() < deadline:
            time.sleep(0.1)

    # ── 同步到 NAS:只加不删;NAS 没挂上就记为待同步,下次补 ────────────────────
    def sync_later(self, sid: str | None = None) -> None:
        threading.Thread(target=self._sync_pending, args=(sid,), daemon=True).start()

    def _sync_pending(self, sid: str | None) -> None:
        with self._sync_lock:
            records = self._sync_records()
            targets = [key for key, record in records.items() if record.get("state") == "pending"]
            for target in dict.fromkeys([sid, *targets] if sid else targets):
                try:
                    state = self._sync_one(self._session(target))
                except EngineError:
                    records.pop(target, None)              # 讨论已不在:不再惦记
                    continue
                if state:
                    records[target] = {"state": state, "at": _now()}
                    self.hub.publish({"type": "view", "id": target})
            write_json(self.paths.sync_log, records)

    def _sync_one(self, session: Session) -> str:
        """返回 synced / pending;没开同步返回空串。"""
        if not self.settings.nas_sync:
            return ""
        target = self._nas_folder()
        if target is None:
            return "pending"
        sources = [str(path) for path in (session.path, session.state_path, session.attachments)
                   if path.exists()]
        try:
            target.mkdir(parents=True, exist_ok=True)
            done = subprocess.run(["/usr/bin/rsync", "-rt", "--", *sources, f"{target}/"],
                                  capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as err:
            log.warning("同步到 NAS 失败:%s", err)
            return "pending"
        if done.returncode != 0:
            log.warning("同步到 NAS 失败:%s", done.stderr.strip()[-300:])
        return "synced" if done.returncode == 0 else "pending"

    def _nas_folder(self) -> Path | None:
        """NAS 上的备份文件夹;没配置、或 NAS 没挂载时返回 None(绝不在挂载点之外建目录)。"""
        cfg = self.settings
        if not (cfg.nas_mount and cfg.nas_dir) or not os.path.ismount(cfg.nas_mount):
            return None
        folder = Path(cfg.nas_dir)
        return folder if Path(cfg.nas_mount) in folder.parents else None

    def _sync_records(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.paths.sync_log.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, dict)} \
            if isinstance(data, dict) else {}

    def _sync_state(self, sid: str) -> dict[str, str]:
        if not self.settings.nas_sync:
            return {"state": "off", "at": ""}
        return self._sync_records().get(sid) or {"state": "none", "at": ""}

    # ── 后台任务 ────────────────────────────────────────────────────────────
    def _continue(self, session: Session, status: Status) -> None:
        if status.phase == Phase.ACTIVE:
            self._start(session, "turn", self._relay)
        else:
            self._changed(session)

    def _start(self, session: Session, job: str, work: Callable[[Session], None]) -> None:
        with session.lock:
            self._require_idle(session)
            session.busy, session.job, session.error = True, job, ""
            session.cancel = threading.Event()
        threading.Thread(target=self._work, args=(session, work), daemon=True).start()
        self._changed(session)

    def _work(self, session: Session, work: Callable[[Session], None]) -> None:
        try:
            work(session)
        except TurnCancelled:
            session.error = "你停止了这一棒。"
        except (ProviderError, EngineError) as err:
            session.error = str(err)
            log.warning("[%s] %s", session.id, err)
        except Exception as err:                            # noqa: BLE001 - 不让后台线程无声死掉
            session.error = f"程序内部出错:{type(err).__name__}: {err}"
            log.exception("[%s] 未预期的错误", session.id)
        finally:
            session.busy, session.job, session.live = False, "", None
            self._changed(session)

    def _relay(self, session: Session) -> None:
        """AI 们依次发言,直到轮到用户。"""
        while True:
            doc = self._read(session)
            status = self._status(doc)
            if status.phase != Phase.ACTIVE:
                return
            member = self.settings.member(status.turn)
            if member is None:
                raise ProviderError(f"成员 {status.turn} 已不在成员名单里。"
                                    "可以点「跳过」,或到设置里把它加回来。")
            self._ai_turn(session, member, doc, status)

    def _ai_turn(self, session: Session, member: Member, doc: Document, status: Status) -> None:
        self._backup(session)
        board = self._board(session)
        provider = self._make_provider(member)
        attached = self._attached(session, doc)
        hands_on = status.assignee == member.id
        speeches = {m.speaker: m.text for m in doc.messages if m.kind == "ai"}   # 各人最近一次发言
        context = prompts.TurnContext(
            speaker=member.name, round=status.round, topic=doc.topic, rules=self._rules(),
            board=whiteboard.render(board), doc_path=str(session.path),
            others=[(self._name(other), speeches[other]) for other in status.members
                    if other != member.id and other in speeches],
            user_name=self.settings.user.name,
            user_text=next((m.text for m in reversed(doc.messages) if m.kind == "user"), ""),
            image_paths=[str(path) for path in attached.images],
            can_see_images=provider.sees_images,
            file_paths=[str(path) for path in attached.files],
            earlier_file_paths=[str(path) for path in attached.earlier_files],
            can_read_files=provider.reads_files,
            assignee=self._name(status.assignee) if status.assignee else "",
            hands_on=hands_on,
            own_text=speeches.get(member.id, "") if hands_on else "",
            protected=[str(folder) for folder in self.paths.protected] if hands_on else [])
        reply, ops, problem = self._ask(session, member, provider, prompts.turn(context),
                                        board, attached.images if provider.sees_images else [],
                                        status="正在干活" if hands_on else "", hands_on=hands_on)
        body, _ = whiteboard.split_reply(reply.text)
        if not body:
            raise ProviderError(f"{member.name} 没有输出任何内容。")
        note = f"⚠ 白板未更新:{problem}" if problem else f"📋 {whiteboard.summarize(ops)}"

        with session.lock:
            raw = session.path.read_text(encoding="utf-8")
            raw = document.with_ai_message(raw, member.id, member.name, status.round, body,
                                           note, _now(), reply.ctx, reply.out, hands_on)
            write_text(session.path, document.with_status(raw, _after(status)))
            whiteboard.apply(board, ops, f"r{status.round}-{member.id.lower()}")
            write_text(session.state_path, whiteboard.render(board))
        self._health[member.id] = {"ctx": reply.ctx, "window": reply.window,
                                   "expires": reply.expires}
        session.live = None
        self._changed(session)

    def _tidy(self, session: Session) -> None:
        status = self._status(self._read(session))
        member = next((m for m in map(self.settings.member, status.members) if m), None)
        if member is None:
            raise ProviderError("这场讨论的成员都已不在名单里,没有人能整理白板。")
        self._backup(session)
        board = self._board(session)
        before = whiteboard.estimate_tokens(whiteboard.render(board))
        _, ops, problem = self._ask(session, member, self._make_provider(member),
                                    prompts.tidy(whiteboard.render(board)), board, [],
                                    status="正在整理白板")
        if problem:
            raise ProviderError(f"{member.name} 给出的整理指令不合格:{problem}")
        whiteboard.apply(board, ops, f"r{status.round}-{member.id.lower()}")
        after = whiteboard.estimate_tokens(whiteboard.render(board))
        note = (f"🧹 {member.name} 整理了白板({whiteboard.summarize(ops)}),"
                f"长度从约 {before} 降到约 {after} token。")
        with session.lock:
            write_text(session.state_path, whiteboard.render(board))
            raw = session.path.read_text(encoding="utf-8")
            write_text(session.path, document.with_note(raw, status.round, note, _now()))

    def _ask(self, session: Session, member: Member, provider: Provider, prompt: str,
             board: whiteboard.Board, images: list[Path], status: str = "",
             hands_on: bool = False) -> tuple[Reply, list[whiteboard.Op], str]:
        """调用 AI 并取出合格的 STATE_OPS;不合格重试一次。返回(回复, 指令, 问题说明)。

        hands_on:这一棒被派了活,可以动手。它从用户主目录出发(活可能在任何项目里),
        时限更长;STATE_OPS 不合格也不重试——重试等于把活再干一遍。"""
        problem = ""
        reply = Reply("")
        for attempt in (1,) if hands_on else (1, 2):
            session.live = Live(member.id, status=status, started=time.time())
            self._publish_live(session, force=True)
            reply = provider.run(Request(
                prompt=prompt if attempt == 1 else prompts.retry(prompt, problem),
                workdir=Path.home() if hands_on else self.paths.discussions,
                env=self._environment(),
                timeout=self.settings.hands_on_timeout if hands_on else self.settings.turn_timeout,
                cancel=session.cancel, images=images,
                hands_on=hands_on, protected=self.paths.protected if hands_on else [],
                on_text=lambda text: self._on_live(session, text=text),
                on_status=lambda text: self._on_live(session, status=text)))
            try:
                return reply, whiteboard.read_ops(reply.text, board), ""
            except whiteboard.BoardError as err:
                problem = str(err)
                log.warning("[%s] %s 的 STATE_OPS 不合格(第 %d 次):%s",
                            session.id, member.name, attempt, problem)
        return reply, [], problem

    def _on_live(self, session: Session, text: str | None = None,
                 status: str | None = None) -> None:
        live = session.live
        if live is None:
            return
        if text is not None:
            live.text = whiteboard.visible_part(text)
        if status is not None:
            live.status = status
        self._publish_live(session)

    def _publish_live(self, session: Session, force: bool = False) -> None:
        now = time.monotonic()
        if session.live and (force or now - self._last_live_event > 0.1):
            self._last_live_event = now
            self.hub.publish({"type": "live", "id": session.id, "live": asdict(session.live)})

    # ── 小工具 ──────────────────────────────────────────────────────────────
    def _is_discussion(self, path: Path) -> bool:
        name = path.name
        return (path.is_file() and name.endswith(".md") and not name.endswith(".state.md")
                and not name.startswith(".") and ".backup." not in name
                and name != self.paths.rules.name)

    def _documents(self) -> Iterator[tuple[Session, Document]]:
        """逐个给出所有讨论;读不了的文件跳过。"""
        for path in self.paths.discussions.glob("*.md"):
            if not self._is_discussion(path):
                continue
            try:
                session = self._session(path.stem)
                yield session, self._read(session)
            except (EngineError, OSError, UnicodeError):
                continue

    def _pins(self) -> list[str]:
        try:
            data = json.loads(self.paths.pins.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []

    def _session(self, sid: str) -> Session:
        path = self.paths.discussions / f"{sid}.md"
        if "/" in sid or "\\" in sid or not self._is_discussion(path):
            raise EngineError("找不到这场讨论。", 404)
        with self._registry_lock:
            return self._sessions.setdefault(sid, Session(sid, path))

    def _read(self, session: Session) -> Document:
        return document.parse(session.path.read_text(encoding="utf-8"))

    def _status(self, doc: Document) -> Status:
        status = doc.status or Status(max_rounds=self.settings.max_rounds)
        return status if status.members else replace(status, members=self.settings.enabled_ids())

    @staticmethod
    def _phase(session: Session, status: Status) -> str:
        return "running" if session.busy else _PHASE_VIEW[status.phase]

    def _require_idle(self, session: Session) -> None:
        if session.busy:
            raise EngineError("AI 正在发言。等它说完,或先点「停止」。", 409)

    def _member_or_user(self, member_id: str) -> Member:
        member = self.settings.user if member_id == USER_ID else self.settings.member(member_id)
        if member is None:
            raise EngineError("找不到这个成员。", 404)
        return member

    def _name(self, member_id: str) -> str:
        if member_id == USER_ID:
            return self.settings.user.name
        member = self.settings.member(member_id)
        return member.name if member else member_id

    def _person(self, member_id: str, last_name: str) -> dict[str, Any]:
        """界面显示一个成员所需的信息;成员已被删掉时用它当年的名字兜底。"""
        member = self.settings.member(member_id)
        if member:
            return {**asdict(member), "missing": False, "assignable": self._assignable(member)}
        name = last_name or member_id
        return {**asdict(Member(member_id, name, "", name[:1], "#8a8a8a")),
                "missing": True, "assignable": False}

    def _assignable(self, member: Member) -> bool:
        """这位成员能不能被派活(由它的类型决定)。"""
        try:
            return self._make_provider(member).assignable
        except ProviderError:
            return False

    def _check_assignee(self, status: Status, assignee: str) -> None:
        """派活的对象必须是这场讨论里能动手的成员,而且这一回还没轮过它。"""
        member = self.settings.member(assignee) if assignee in status.members else None
        if member is None:
            raise EngineError("派活的对象不在这场讨论的成员里。")
        if not self._assignable(member):
            raise EngineError(f"{member.name} 只能发言,不能被派活。")
        order = status.members
        if status.phase == Phase.ACTIVE and status.turn in order \
                and order.index(assignee) < order.index(status.turn):
            raise EngineError(f"{member.name} 这一回已经发过言了。等这一回结束、轮到你时再派活给它。")

    def _rules(self) -> str:
        try:
            return self.paths.rules.read_text(encoding="utf-8")
        except OSError:
            return prompts.DEFAULT_RULES

    def _board(self, session: Session) -> whiteboard.Board:
        if not session.state_path.exists():
            return whiteboard.Board()
        try:
            return whiteboard.parse(session.state_path.read_text(encoding="utf-8"))
        except whiteboard.BoardError as err:
            raise EngineError(f"{err}:{session.state_path.name}。请从备份恢复后再继续。", 500) from err

    def _board_or_blank(self, session: Session) -> tuple[whiteboard.Board, str]:
        try:
            return self._board(session), ""
        except EngineError as err:
            return whiteboard.Board(), str(err)

    def _attached(self, session: Session, doc: Document) -> Attached:
        """用户附给 AI 看的图片和文件。图片只算最近一次发言(还没发过言则是议题)附的;
        文件另外带上更早附过的,因为一份资料往往要讨论好几回。"""
        folder = session.attachments.resolve()

        def existing(relatives: list[str]) -> list[Path]:
            """这些路径里,确实是本场附件夹里的文件的那些(去掉重复)。"""
            paths = [(self.paths.discussions / relative).resolve() for relative in relatives]
            return [path for path in dict.fromkeys(paths)
                    if path.parent == folder and path.is_file()]

        said = [m.text for m in doc.messages if m.kind in ("user", "topic")]    # 从早到晚
        latest = said[-1] if said else ""
        files = existing(document.file_paths(latest))
        earlier = existing([path for text in said[:-1] for path in document.file_paths(text)])
        return Attached(images=existing(document.image_paths(latest)), files=files,
                        earlier_files=[path for path in earlier if path not in files])

    def _backup(self, session: Session) -> None:
        """改动前留一份回滚点,每场讨论只保留最近若干份。"""
        folder = self.paths.backups / session.id
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(session.path, folder / f"{stamp}.md")
        if session.state_path.exists():
            shutil.copy2(session.state_path, folder / f"{stamp}.state.md")
        stamps = sorted({path.name.split(".")[0] for path in folder.iterdir()})
        for old in stamps[:-self.settings.keep_backups]:
            for path in folder.glob(f"{old}.*"):
                path.unlink()

    def _environment(self) -> dict[str, str]:
        """子进程的环境变量:不带任何 API key(只走订阅);按设置决定走不走代理。"""
        env = {key: value for key, value in os.environ.items()
               if not key.upper().endswith("_API_KEY") and key not in _PROXY_KEYS}
        paths = _EXTRA_PATH + env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
        proxy = self.settings.proxy
        if proxy and _reachable(proxy):
            env.update({key: proxy for key in _PROXY_KEYS})
            env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        elif proxy:
            log.warning("代理 %s 没有响应,这次改为直连。", proxy)
        return env

    def _changed(self, session: Session) -> None:
        self.hub.publish({"type": "view", "id": session.id})
        self.hub.publish({"type": "list"})


# ── 与具体讨论无关的小函数 ──────────────────────────────────────────────────
def _after(status: Status) -> Status:
    """这一棒结束后轮到谁:下一个 AI;AI 都说完了就轮到用户。"""
    order = status.members
    position = order.index(status.turn) if status.turn in order else len(order) - 1
    if position + 1 < len(order):
        return replace(status, turn=order[position + 1], updated=_now())
    return replace(status, phase=Phase.USER, turn=USER_ID, assignee="", updated=_now())


def _image_extension(data: bytes) -> str:
    """内容是 PNG / JPG 就返回扩展名,否则返回空串。只看文件内容,不信文件名。"""
    return next((extension for magic, extension in _IMAGE_TYPES.items()
                 if data.startswith(magic)), "")


def _free_name(path: Path) -> Path:
    """path 已被占用时,在文件名后加序号,直到找到一个空位。"""
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} {number}{path.suffix}")
        number += 1
    return candidate


def _existing_child(folder: Path, name: str) -> Path:
    path = folder / Path(name).name
    if not path.is_file():
        raise EngineError("找不到这个文件。", 404)
    return path


def _reachable(proxy: str) -> bool:
    """代理地址上是否有程序在接听。"""
    parsed = urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.3):
            return True
    except OSError:
        return False
