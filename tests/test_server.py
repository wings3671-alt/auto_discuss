"""网页服务:通过真实的 HTTP 请求走一遍主要接口。"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from discuss.config import Paths
from discuss.engine import Engine
from discuss.server import serve
from tests.test_engine import NO_OPS, PNG, FakeAI


class ServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = Paths(root, trash=root / "废纸篓", downloads=root / "下载")
        paths.ensure()
        paths.settings.write_text(json.dumps({"nas_sync": False}), encoding="utf-8")
        self.engine = Engine(paths, lambda member: FakeAI(member, {}, []), reveal=lambda path: None)
        self.server = serve(self.engine, 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + urllib.parse.quote(path, safe="/?=&"), data=data,
                                         method=method, headers=headers or {})
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.headers.get_content_type(), response.read()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.headers.get_content_type(), err.read()

    def json(self, *args, **kwargs):
        status, _, payload = self.call(*args, **kwargs)
        return status, json.loads(payload)

    def test_pages_are_served(self):
        for path, kind in (("/", "text/html"), ("/web/app.css", "text/css"),
                           ("/web/app.js", "text/javascript"), ("/web/markdown.js", "text/javascript")):
            status, content_type, payload = self.call("GET", path)
            self.assertEqual((status, content_type), (200, kind), path)
            self.assertTrue(payload)
        self.assertEqual(self.call("GET", "/web/../config.py")[0], 404)

    def test_page_opens_in_the_chosen_theme(self):
        self.assertIn(b'<html data-theme="ink" lang="zh-CN">', self.call("GET", "/")[2])
        settings = self.json("GET", "/api/settings")[1]
        settings["theme"] = "slate"
        self.json("PUT", "/api/settings", settings)
        self.assertIn(b'<html data-theme="slate" lang="zh-CN">', self.call("GET", "/")[2])

    def test_whole_discussion_over_http(self):
        status, created = self.json("POST", "/api/discussions", {"title": "遮阳棚"})
        sid = created["id"]
        status, image = self.json("POST", f"/api/discussions/{sid}/attachments", raw=PNG,
                                  headers={"X-Filename": urllib.parse.quote("现状.png")})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", f"/api/discussions/{sid}/files/{image['name']}")[:2],
                         (200, "image/png"))
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/send",
                                   {"text": f"要不要装?\n\n{image['markdown']}"}), (200, {"ok": True}))
        deadline = time.monotonic() + 5
        while self.json("GET", f"/api/discussions/{sid}")[1]["phase"] != "user":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        status, view = self.json("GET", f"/api/discussions/{sid}")
        self.assertEqual([m["kind"] for m in view["messages"]], ["topic", "ai", "ai"])
        self.assertEqual(self.json("GET", "/api/discussions")[1][0]["phase"], "user")
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/rename", {"title": "新名"})[0], 200)
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/pin", {"pinned": True})[0], 200)
        self.assertTrue(self.json("GET", "/api/discussions")[1][0]["pinned"])
        (hit,) = self.json("GET", "/api/search?q=要不要")[1]
        self.assertEqual(hit["id"], sid)
        self.assertTrue(hit["snippet"].startswith("要不要装?"))
        status, saved = self.json("POST", f"/api/discussions/{sid}/export", raw="<p>页</p>".encode())
        self.assertEqual((status, Path(saved["path"]).name), (200, "新名.html"))
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/trash", {"nas": False}), (200, {"nas": "kept"}))
        self.assertEqual(self.json("GET", "/api/discussions")[1], [])

    def test_assignee_is_passed_through(self):
        sid = self.json("POST", "/api/discussions", {"title": "x"})[1]["id"]
        status, answer = self.json("POST", f"/api/discussions/{sid}/send",
                                   {"text": "干活", "assignee": "NOBODY"})
        self.assertEqual((status, answer), (400, {"error": "派活的对象不在这场讨论的成员里。"}))
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/send",
                                   {"text": "干活", "assignee": "CLAUDE"}), (200, {"ok": True}))

    def test_errors_are_readable_json(self):
        self.assertEqual(self.json("GET", "/api/discussions/不存在"), (404, {"error": "找不到这场讨论。"}))
        self.assertEqual(self.json("POST", "/api/discussions", {"title": 5})[0], 400)
        self.assertEqual(self.json("POST", "/api/discussions", raw=b"not json")[0], 400)
        self.assertEqual(self.json("GET", "/api/nothing")[0], 404)
        sid = self.json("POST", "/api/discussions", {"title": "x"})[1]["id"]
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/attachments", raw=b"")[0], 400)
        status, saved = self.json("POST", f"/api/discussions/{sid}/attachments", raw=b"%PDF",
                                  headers={"X-Filename": urllib.parse.quote("报价.pdf")})
        self.assertEqual((status, saved["label"], saved["image"]), (200, "报价.pdf", False))
        self.assertEqual(self.call("GET", f"/api/discussions/{sid}/files/{saved['name']}")[0], 404)
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/reveal", {"name": saved["name"]})[0], 200)
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/reveal", {"name": "没有.pdf"})[0], 404)
        self.assertEqual(self.json("POST", f"/api/discussions/{sid}/explode")[0], 404)

    def test_requests_from_other_sites_are_refused(self):
        self.assertEqual(self.json("GET", "/api/discussions", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.json("GET", "/api/discussions", headers={"Host": "evil.example"})[0], 403)
        own = {"Origin": self.base}
        self.assertEqual(self.json("GET", "/api/discussions", headers=own)[0], 200)

    def test_settings_roundtrip(self):
        status, settings = self.json("GET", "/api/settings")
        settings["user"]["name"] = "老大"
        settings["rules"] = "## 新守则\n1. 说人话。"
        status, saved = self.json("PUT", "/api/settings", settings)
        self.assertEqual((status, saved["user"]["name"]), (200, "老大"))
        self.assertEqual(saved["rules"].strip(), "## 新守则\n1. 说人话。")
        status, avatar = self.json("POST", "/api/avatars/CLAUDE", raw=PNG)
        self.assertEqual(self.call("GET", f"/api/avatars/{avatar['image']}")[:2], (200, "image/png"))
        self.assertEqual(self.json("GET", "/api/settings")[1]["members"][0]["image"], avatar["image"])

    def test_event_stream_announces_changes(self):
        request = urllib.request.Request(self.base + "/api/events")
        with self.opener.open(request, timeout=5) as stream:
            self.assertEqual(stream.readline(), b"retry: 1500\n")
            stream.readline()
            self.json("POST", "/api/discussions", {"title": "x"})
            self.assertEqual(json.loads(stream.readline()[6:]), {"type": "list"})


if __name__ == "__main__":
    unittest.main()
