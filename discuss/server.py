"""本机网页服务:界面通过它读写。只监听 127.0.0.1,别的设备连不进来。

    GET    /                                   界面
    GET    /api/discussions                    讨论列表
    POST   /api/discussions                    新建          {title}
    GET    /api/discussions/<id>               一场讨论的全部内容
    POST   /api/discussions/<id>/send          用户发言      {text, finish, extend}
    POST   /api/discussions/<id>/<动作>         resume / skip / stop / tidy / sync / trash
    POST   /api/discussions/<id>/rename        改标题        {title}
    POST   /api/discussions/<id>/attachments   上传附图(请求体即图片)
    GET    /api/discussions/<id>/files/<名>    读附图
    GET    /api/settings      PUT /api/settings
    POST   /api/avatars/<成员>   GET /api/avatars/<名>
    GET    /api/events                         变化通知(SSE 长连接)
"""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .engine import Engine, EngineError

log = logging.getLogger("discuss")

WEB_DIR = Path(__file__).resolve().parent / "web"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024
_SIMPLE_ACTIONS = ("resume", "skip", "stop", "tidy", "trash")


def serve(engine: Engine, port: int) -> ThreadingHTTPServer:
    """在 port 上启动服务;端口被占用就依次试后面的 20 个。"""
    last_error: OSError | None = None
    for candidate in range(port, port + 21):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), _handler_for(engine))
        except OSError as err:
            last_error = err
            continue
        server.daemon_threads = True
        return server
    raise OSError(f"端口 {port}–{port + 20} 都被占用") from last_error


def _handler_for(engine: Engine) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ── 入口 ────────────────────────────────────────────────────────────
        def do_GET(self) -> None:
            self._handle(self._get)

        def do_POST(self) -> None:
            self._handle(self._post)

        def do_PUT(self) -> None:
            self._handle(self._put)

        def _handle(self, route: Callable[[list[str]], None]) -> None:
            try:
                self._check_origin()
                route([unquote(part) for part in urlparse(self.path).path.split("/") if part])
            except EngineError as err:
                self._json({"error": str(err)}, err.code)
            except (BrokenPipeError, ConnectionResetError):
                pass                                         # 界面关了,不算错误
            except Exception as err:                         # noqa: BLE001
                log.exception("处理请求出错:%s %s", self.command, self.path)
                self._json({"error": f"程序内部出错:{type(err).__name__}: {err}"}, 500)

        def _check_origin(self) -> None:
            """只接受从本机界面发来的请求,挡住别的网页借浏览器偷偷调用。"""
            allowed = {f"{host}:{self.server.server_port}" for host in ("127.0.0.1", "localhost")}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in allowed or (
                    origin and urlparse(origin).netloc not in allowed):
                raise EngineError("拒绝访问。", 403)

        # ── 路由 ────────────────────────────────────────────────────────────
        def _get(self, parts: list[str]) -> None:
            if not parts:
                return self._file(WEB_DIR / "index.html")
            if parts[0] == "web" and len(parts) == 2:
                return self._file(WEB_DIR / Path(parts[1]).name)
            if parts[0] != "api":
                raise EngineError("没有这个页面。", 404)
            rest = parts[1:]
            if rest == ["discussions"]:
                return self._json(engine.summaries())
            if rest == ["settings"]:
                return self._json(engine.settings_view())
            if rest == ["events"]:
                return self._events()
            if len(rest) == 2 and rest[0] == "discussions":
                return self._json(engine.view(rest[1]))
            if len(rest) == 4 and rest[0] == "discussions" and rest[2] == "files":
                return self._file(engine.attachment(rest[1], rest[3]))
            if len(rest) == 2 and rest[0] == "avatars":
                return self._file(engine.avatar(rest[1]))
            raise EngineError("没有这个接口。", 404)

        def _post(self, parts: list[str]) -> None:
            rest = parts[1:] if parts[:1] == ["api"] else []
            if rest == ["discussions"]:
                return self._json({"id": engine.create(_text(self._body(), "title"))})
            if len(rest) == 2 and rest[0] == "avatars":
                return self._json({"image": engine.set_avatar(rest[1], self._upload())})
            if len(rest) != 3 or rest[0] != "discussions":
                raise EngineError("没有这个接口。", 404)
            sid, action = rest[1], rest[2]
            if action == "send":
                body = self._body()
                return self._json(engine.send(
                    sid, _text(body, "text"), finish=body.get("finish") is True,
                    extend=body.get("extend") is True))
            if action == "rename":
                engine.rename(sid, _text(self._body(), "title"))
            elif action == "attachments":
                name = unquote(self.headers.get("X-Filename") or "图片")
                return self._json(engine.attach(sid, name, self._upload()))
            elif action == "sync":
                engine.sync_later(sid)
            elif action in _SIMPLE_ACTIONS:
                getattr(engine, action)(sid)
            else:
                raise EngineError("没有这个接口。", 404)
            self._json({"ok": True})

        def _put(self, parts: list[str]) -> None:
            if parts != ["api", "settings"]:
                raise EngineError("没有这个接口。", 404)
            engine.update_settings(self._body())
            self._json(engine.settings_view())

        # ── 读请求 ──────────────────────────────────────────────────────────
        def _raw_body(self, limit: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > limit:
                raise EngineError("内容太大了。", 413)
            return self.rfile.read(length) if length > 0 else b""

        def _body(self) -> dict[str, Any]:
            try:
                data = json.loads(self._raw_body(_MAX_JSON_BYTES) or b"{}")
            except ValueError as err:
                raise EngineError("请求格式不对。") from err
            if not isinstance(data, dict):
                raise EngineError("请求格式不对。")
            return data

        def _upload(self) -> bytes:
            return self._raw_body(_MAX_UPLOAD_BYTES)

        # ── 写响应 ──────────────────────────────────────────────────────────
        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, "application/json; charset=utf-8",
                       json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def _file(self, path: Path) -> None:
            if not path.is_file():
                raise EngineError("找不到这个文件。", 404)
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind.endswith(("javascript", "json")):
                kind += "; charset=utf-8"
            self._send(200, kind, path.read_bytes())

        def _send(self, code: int, content_type: str, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _events(self) -> None:
            """长连接:后台一有变化就推一条消息;安静时每 15 秒发一次心跳。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            listener = engine.hub.subscribe()
            try:
                self.wfile.write(b"retry: 1500\n\n")
                while True:
                    try:
                        event = listener.get(timeout=15)
                        line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        line = ": ping\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            finally:
                engine.hub.unsubscribe(listener)

        def log_message(self, format: str, *args: Any) -> None:
            return None                                       # 不往终端刷访问日志

    return Handler


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise EngineError(f"{key} 必须是文字。")
    return value
