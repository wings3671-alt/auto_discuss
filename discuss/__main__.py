"""启动入口:python3 -m discuss

正常使用时由「讨论群.app」启动本程序;也可以在终端里直接运行,再用浏览器打开
它打印出来的地址。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Paths, project_root
from .engine import Engine
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="讨论群:多个 AI 与你轮流讨论")
    parser.add_argument("--root", type=Path, default=project_root(),
                        help="数据所在的文件夹(默认是程序所在的项目文件夹)")
    parser.add_argument("--port", type=int, help="监听端口(默认用设置里的)")
    parser.add_argument("--follow-parent", action="store_true",
                        help="启动它的程序退出后,自己也退出")
    args = parser.parse_args()

    paths = Paths(args.root.resolve())
    paths.ensure()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(paths.log, maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")])

    engine = Engine(paths)
    server = serve(engine, args.port or engine.settings.port)
    engine.sync_later()                       # 补上之前 NAS 没挂载时欠下的同步
    print(f"READY {server.server_port}", flush=True)
    logging.info("已启动:http://127.0.0.1:%d", server.server_port)

    def stop(*_: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.follow_parent:
        threading.Thread(target=_exit_with_parent, args=(stop,), daemon=True).start()

    server.serve_forever()
    engine.shutdown()
    logging.info("已退出")


def _exit_with_parent(stop) -> None:
    parent = os.getppid()
    while os.getppid() == parent:
        time.sleep(1)
    stop()


if __name__ == "__main__":
    main()
