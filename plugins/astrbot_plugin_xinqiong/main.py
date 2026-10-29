# -*- coding: utf-8 -*-
# Copyright (C) 2026 心穹中枢
# SPDX-License-Identifier: AGPL-3.0-or-later
"""心穹中枢：把 Atri控制台 生成的星图页面镜像成独立入口。

为什么单独做一个插件：
AstrBot 的侧边栏是「一个插件一个入口」，页面页签再多也只显示其中一个。
要让「心穹中枢」在扩展功能里单独列出来，只能让它自己成为一个插件。

页面本体（index.html / data.json / nav.js）由 astrbot_plugin_console 生成，
这里只做定时镜像，不重复实现任何渲染逻辑。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_xinqiong"

PLUGINS_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PLUGINS_DIR / "astrbot_plugin_console" / "pages" / "star"
DST_DIR = Path(__file__).resolve().parent / "pages" / "main"

MIRROR_FILES = ("index.html", "data.json")


def mirror_once() -> str:
    """把控制台生成的星图页面复制一份过来。"""
    if not SRC_DIR.is_dir():
        return "源目录不存在"
    DST_DIR.mkdir(parents=True, exist_ok=True)
    stale_nav = DST_DIR / "nav.js"
    if stale_nav.is_file():
        try:
            stale_nav.unlink()
        except Exception:
            pass
    copied = 0
    for name in MIRROR_FILES:
        src = SRC_DIR / name
        if not src.is_file():
            continue
        dst = DST_DIR / name
        try:
            # data.json 用「大小 + 修改时间」判断，没变就跳过；index.html 每次重新复制
            if name == "data.json" and dst.is_file():
                ss, ds = src.stat(), dst.stat()
                if ss.st_size == ds.st_size and int(ss.st_mtime) == int(ds.st_mtime):
                    continue
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copy2(src, tmp)

            # 页面正被 WebUI 读取时 Windows 会锁文件（WinError 32），重试几次
            last = None
            for attempt in range(6):
                try:
                    tmp.replace(dst)
                    last = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    time.sleep(0.25)
            if last is not None:
                raise last
            copied += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 复制 {name} 失败: {exc}")
    return f"更新 {copied} 个文件"


@register(PLUGIN_NAME, "Atri", "她的记忆星空：一个球形的记忆图谱", "v1.0.0")
class XinQiongPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config
        self._task = None
        try:
            info = mirror_once()
            logger.info(f"[{PLUGIN_NAME}] 初始镜像：{info}")
            self._task = asyncio.create_task(self._loop())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 初始化失败: {exc}")

    async def _loop(self) -> None:
        """控制台每 30 秒重写 data.json，这里也跟着同步（页面自己还会轮询 data.json）。"""
        while True:
            try:
                await asyncio.sleep(20)
                await asyncio.to_thread(mirror_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 镜像异常: {exc}")
