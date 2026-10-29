# -*- coding: utf-8 -*-
# Copyright (C) 2026 心穹中枢
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atri控制台 —— 把 Tk 控制台的日常页搬进 AstrBot WebUI。

页面（WebUI → 扩展功能 → Atri控制台）：
  logs    日志尾部（2s 轮询、暂停、过滤）
  status  运行状态看板（端口/账号/进程/余额/今日用量/人设/记忆/插件）
  ops     运维（重启、体检）

约定（踩过的坑，见 knowledge/astrbot-plugin-page-dev.md）：
  * 路由必须以 /<插件名>/ 开头
  * 返回用 {status, data} 信封 —— 宿主会脱一层再递给页面
  * 所有阻塞操作（socket/urllib/sqlite/subprocess）都扔进线程，别卡 bot 的事件循环
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
import re
import re as _re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import os as _os

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request

PLUGIN_NAME = "astrbot_plugin_console"

_PLUGIN_DIR = Path(__file__).resolve().parent


def _atri_root():
    """AstrBot 根目录：优先环境变量 ATRI_ROOT，其次 AstrBot 的 data 目录上一层。"""
    env = _os.environ.get("ATRI_ROOT")
    if env:
        return Path(env).resolve()
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()).parent.resolve()
    except Exception:
        # 最后兜底：按 <root>/data/plugins/<plugin> 反推两层
        return _PLUGIN_DIR.parents[2] if len(_PLUGIN_DIR.parents) > 2 else _PLUGIN_DIR


ATR_ROOT = _atri_root()
DATA_DIR = ATR_ROOT / "data"          # <root>/data
ASTRBOT_DIR = ATR_ROOT                # <root>
BOTDATA = ATR_ROOT / "botdata"
DB_FILE = DATA_DIR / "data_v4.db"
NAPCAT_API = "http://127.0.0.1:3000"
DASHBOARD = "http://127.0.0.1:6185"
PORTS = (6185, 6199, 6099, 3000)

MAX_LINES_CEILING = 2000
MAX_BYTES_CEILING = 4 * 1024 * 1024
BALANCE_TTL = 300  # 秒


# ---------------------------------------------------------------- 只读小工具


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _window_pid(image: str) -> str:
    """tasklist 查进程 PID（Windows；非 Windows 直接返回空）。"""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {image}", "/fo", "csv", "/nh"],
            capture_output=True,
            timeout=15,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return ""
    for line in out.splitlines():
        if image.lower() not in line.lower():
            continue
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            return parts[1]
    return ""


def _today_stats() -> tuple[int, int]:
    if not DB_FILE.exists():
        return 0, 0
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT count(*),"
                " coalesce(sum(token_input_other),0)+coalesce(sum(token_input_cached),0)"
                " FROM provider_stats"
                " WHERE substr(created_at,1,10)=date('now','localtime')"
            ).fetchone()
        finally:
            con.close()
        return int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return 0, 0


def _personas() -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=3)
        try:
            rows = con.execute(
                "SELECT persona_id, length(system_prompt) FROM personas"
                " ORDER BY length(system_prompt) DESC"
            ).fetchall()
        finally:
            con.close()
        return [{"id": str(r[0]), "chars": int(r[1] or 0)} for r in rows]
    except Exception:
        return []


def _memory_count() -> int:
    total = 0
    try:
        for path in (BOTDATA / "memory").glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list):
                total += len(items)
    except Exception:
        pass
    return total


def _deepseek_key() -> str:
    try:
        cfg = json.loads((DATA_DIR / "cmd_config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    for source in cfg.get("provider_sources") or []:
        for key in source.get("key") or []:
            if isinstance(key, str) and key.startswith("sk-"):
                return key
    return ""


def _balance(key: str) -> str:
    if not key:
        return "查不到"
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        infos = payload.get("balance_infos") or []
        if infos:
            return f"¥{infos[0].get('total_balance', '?')}"
    except Exception:
        pass
    return "查不到"


def _tail_text(path: Path, max_lines: int, max_bytes: int) -> tuple[list[str], bool]:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(max_bytes)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    truncated = start > 0 or len(lines) > max_lines
    return lines[-max_lines:], truncated


# ---------------------------------------------------------------- 配置读写（带快照）

CONFIG_DIR = DATA_DIR / "config"
SNAPSHOT_CMD = ATR_ROOT / "_meta" / "snapshot.cmd"


def _jread(path: Path, default=None):
    """AstrBot 写配置时带 UTF-8 BOM，必须用 utf-8-sig 读，否则整份配置读成空。"""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {} if default is None else default


def _jwrite(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def _snapshot(path: Path, note: str) -> tuple[bool, str]:
    """写盘前快照（_meta/snapshot.cmd）。新建文件无需快照。"""
    if not path.exists():
        return True, "新建文件，无需快照"
    try:
        proc = subprocess.run(
            ["cmd", "/c", str(SNAPSHOT_CMD), "-Path", str(path), "-Note", note],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ATR_ROOT),
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _dig(data, path: list[str]):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _put(data: dict, path: list[str], value) -> None:
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _as_list(text) -> list[str]:
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    raw = str(text or "").replace("，", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


# 群聊与记忆页：字段规格（服务端下发，页面按它渲染表单）
GROUPS_SPEC = [
    {
        "file": "astrbot_plugin_group_banter_config.json",
        "title": "群聊接话",
        "note": "改完立即生效（插件每次读配置）",
        "fields": [
            {"key": "enable", "path": ["enable"], "type": "bool", "label": "启用接话"},
            {"key": "chance", "path": ["chance"], "type": "percent", "label": "随机接话概率", "hint": "填 3 表示 3%"},
            {"key": "quiet_start", "path": ["quiet_start"], "type": "int", "label": "夜间开始(点)", "min": 0, "max": 23},
            {"key": "quiet_end", "path": ["quiet_end"], "type": "int", "label": "夜间结束(点)", "min": 0, "max": 23},
            {"key": "quiet_chance", "path": ["quiet_chance"], "type": "percent", "label": "夜间接话概率", "hint": "0.5 表示 0.5%"},
            {"key": "burst_chance", "path": ["burst_chance"], "type": "percent", "label": "陪聊续话概率"},
            {"key": "burst_turns", "path": ["burst_turns"], "type": "int", "label": "最多再聊几句", "min": 0, "max": 20},
            {"key": "burst_seconds", "path": ["burst_seconds"], "type": "int", "label": "陪聊窗口(秒)", "min": 30, "max": 7200},
            {"key": "min_gap_seconds", "path": ["min_gap_seconds"], "type": "int", "label": "最小间隔(秒)", "min": 2, "max": 300},
            {"key": "max_sentences", "path": ["max_sentences"], "type": "int", "label": "单次最多几句", "min": 1, "max": 20},
            {"key": "keywords", "path": ["keywords"], "type": "list", "label": "关键词（命中必回）"},
            {"key": "groups", "path": ["groups"], "type": "list", "label": "生效群号"},
        ],
    },
    {
        "file": "astrbot_plugin_group_noat_config.json",
        "title": "免@群",
        "note": "每条消息都唤醒，很费 token",
        "fields": [
            {"key": "enable", "path": ["enable"], "type": "bool", "label": "启用免@"},
            {"key": "groups", "path": ["groups"], "type": "list", "label": "免@群号"},
        ],
    },
    {
        "file": "astrbot_plugin_memory_lib_config.json",
        "title": "记忆库注入",
        "note": "注入条数/字符上限决定每轮塞多少记忆",
        "fields": [
            {"key": "enable", "path": ["enable"], "type": "bool", "label": "启用注入"},
            {"key": "inject_in_group", "path": ["inject_in_group"], "type": "bool", "label": "群聊也注入"},
            {"key": "max_items", "path": ["max_items"], "type": "int", "label": "最多注入条数", "min": 10, "max": 1000},
            {"key": "max_chars", "path": ["max_chars"], "type": "int", "label": "字符上限", "min": 500, "max": 20000},
        ],
    },
    {
        "file": "astrbot_plugin_proactive_talk_config.json",
        "title": "主动搭话",
        "note": "群/私聊分别控制；概率按百分比填",
        "fields": [
            {"key": "enable", "path": ["enable"], "type": "bool", "label": "启用主动搭话"},
            {"key": "group_enable", "path": ["group_enable"], "type": "bool", "label": "群里也搭话"},
            {"key": "probability", "path": ["probability"], "type": "percent", "label": "触发概率"},
            {"key": "cooldown_seconds", "path": ["cooldown_seconds"], "type": "int", "label": "冷却(秒)", "min": 60, "max": 86400},
            {"key": "daily_limit", "path": ["daily_limit"], "type": "int", "label": "每日上限", "min": 0, "max": 100},
            {"key": "min_idle_seconds", "path": ["min_idle_seconds"], "type": "int", "label": "静默起点(秒)", "min": 30, "max": 7200},
        ],
    },
    {
        "file": "astrbot_plugin_meme_manager_config.json",
        "title": "表情包配图",
        "note": "改完需要重载插件才生效",
        "fields": [
            {"key": "meme_prob", "path": ["generation", "emotion", "probability"], "type": "int", "label": "配图概率(%)", "min": 1, "max": 100},
        ],
    },
    {
        "file": "voice_switch.json",
        "title": "语音",
        "note": "关掉后不再合成语音（文字照常）",
        "fields": [
            {"key": "enable", "path": ["enable"], "type": "bool", "label": "启用语音"},
        ],
    },
]


def _spec_file(spec: dict) -> Path:
    name = spec["file"]
    return (BOTDATA / name) if name == "voice_switch.json" else (CONFIG_DIR / name)


def _groups_payload() -> list[dict]:
    out = []
    for spec in GROUPS_SPEC:
        path = _spec_file(spec)
        data = _jread(path, {})
        fields = []
        for field in spec["fields"]:
            raw = _dig(data, field["path"])
            if field["type"] == "percent":
                try:
                    value = round(float(raw) * 100, 3)
                except (TypeError, ValueError):
                    value = 0.0
            elif field["type"] == "list":
                value = _as_list(raw)
            elif field["type"] == "bool":
                value = bool(raw)
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    value = field.get("min", 0)
            fields.append({**field, "value": value})
        out.append({"title": spec["title"], "note": spec["note"], "file": spec["file"], "fields": fields})
    return out


def _groups_apply(payload: dict) -> dict:
    """把页面提交的值写回各配置文件（每个文件写前快照）。返回写入摘要。"""
    written = []
    for spec in GROUPS_SPEC:
        path = _spec_file(spec)
        data = _jread(path, {})
        group_in = (payload.get(spec["file"]) or {}) if isinstance(payload, dict) else {}
        for field in spec["fields"]:
            if field["key"] not in group_in:
                continue
            raw = group_in[field["key"]]
            kind = field["type"]
            if kind == "bool":
                value = bool(raw)
            elif kind == "percent":
                try:
                    value = max(0.0, min(1.0, float(raw) / 100.0))
                except (TypeError, ValueError):
                    continue
            elif kind == "list":
                value = _as_list(raw)
            else:
                try:
                    num = int(float(raw))
                except (TypeError, ValueError):
                    continue
                value = max(int(field.get("min", -10**9)), min(int(field.get("max", 10**9)), num))
            _put(data, field["path"], value)
        # 浮动项：Tk 那边一直是关的，保持关
        if spec["file"] == "astrbot_plugin_group_banter_config.json":
            data["chance_jitter_enabled"] = False
            data["chance_jitter"] = 0.0
        if spec["file"] == "astrbot_plugin_proactive_talk_config.json":
            data["probability_jitter_enabled"] = False
            data["probability_jitter"] = 0.0
        if spec["file"] == "astrbot_plugin_meme_manager_config.json":
            emo = _dig(data, ["generation", "emotion", "probability_jitter_enabled"])
            if emo is None:
                _put(data, ["generation", "emotion", "probability_jitter_enabled"], False)
                _put(data, ["generation", "emotion", "probability_jitter"], 0)

        ok, msg = _snapshot(path, "webui-groups")
        if not ok:
            raise RuntimeError(f"快照失败，未写入 {path.name}：{msg}")
        _jwrite(path, data)
        written.append(path.name)
    return {"files": written}


# ---------------------------------------------------------------- 记忆库

MEMORY_DIR = BOTDATA / "memory"


_GRAPH_STOP = set("的了是在我很就都你不他她它这那有和也而与被把给对从在吗呢吧啊哦嗯")


CONSOLE_PAGES = (
    "status", "logs", "ops", "groups", "memory", "slang",
    "content", "maint", "period", "growth", "persona", "star",
)


def _mint_page_url(view: str, days: int = 7) -> str:
    """自己签一个 plugin_page_asset 令牌，拼出该页面的 content_path。

    沙箱 iframe 里既不能改 parent.location，也不能复用别的页面的令牌
    （令牌里带 page_name），所以只能由插件预先签发好每个页面的地址。
    """
    import jwt as _jwt

    cfg = json.loads((DATA_DIR / "cmd_config.json").read_text(encoding="utf-8-sig"))
    dash = cfg.get("dashboard") or {}
    secret = str(dash.get("jwt_secret") or "")
    user = str(dash.get("username") or "")
    if not secret:
        raise RuntimeError("没有 dashboard.jwt_secret")
    now = int(time.time())
    payload = {
        "username": user,
        "token_type": "plugin_page_asset",
        "purpose": "page_session",
        "plugin_name": PLUGIN_NAME,
        "page_name": view,
        "locale": "zh-CN",
        "iat": now,
        "exp": now + days * 86400,
    }
    token = _jwt.encode(payload, secret, algorithm="HS256")
    return f"/api/v1/plugins/{PLUGIN_NAME}/views/{view}/_t/{token}/"


def _write_nav_js() -> tuple[bool, str]:
    """把 12 个页面的地址写进每个页面目录的 nav.js（页签靠它跳转）。"""
    try:
        urls = {view: _mint_page_url(view) for view in CONSOLE_PAGES}
        body = "window.__NAV__ = " + json.dumps(urls, ensure_ascii=False) + ";\n"
        written = 0
        for view in CONSOLE_PAGES:
            target = DATA_DIR / "plugins" / PLUGIN_NAME / "pages" / view / "nav.js"
            if not target.parent.is_dir():
                continue
            target.write_text(body, encoding="utf-8")
            written += 1
        return True, f"{written} 个页面"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _verify_dashboard_token(token: str) -> bool:
    """校验 WebUI 的 JWT（和 dashboard 用同一个 secret）。"""
    try:
        import jwt as _jwt

        cfg = json.loads((DATA_DIR / "cmd_config.json").read_text(encoding="utf-8-sig"))
        secret = str((cfg.get("dashboard") or {}).get("jwt_secret") or "")
        if not secret:
            return False
        _jwt.decode(token, secret, algorithms=["HS256"])
        return True
    except Exception:
        return False


_DATE_RE = None


def _entry_date(item: dict, path) -> str:
    """条目发生在哪一天：id 前缀 > 文件名的日期 > last_seen > created_at。"""
    import re as _re
    global _DATE_RE
    if _DATE_RE is None:
        _DATE_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2})")
    for raw in (item.get("id"), getattr(path, "stem", ""), item.get("last_seen"),
                item.get("created_at")):
        m = _DATE_RE.match(str(raw or "").strip())
        if m:
            return m.group(1)
    return ""


def _memory_graph() -> dict:
    """记忆星图：点=记忆，亮度=清晰度，大小=被提起次数，连线=共享的实词。

    连边：把标题+正文切成 2~3 字的中文 n-gram，去掉停用词，
    只保留「2 <= 文档频率 <= 30%」的词作为特征词，共享 ≥2 个特征词的记忆之间连一条边。
    """
    nodes: list[dict] = []
    if not MEMORY_DIR.exists():
        return {"nodes": [], "links": []}

    def grams(text: str) -> set[str]:
        cleaned = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", " ", text or "")
        out: set[str] = set()
        for piece in cleaned.split():
            if _re.fullmatch(r"[0-9A-Za-z]+", piece):
                if 3 <= len(piece) <= 12:
                    out.add(piece.lower())
                continue
            for n in (2, 3):
                for i in range(len(piece) - n + 1):
                    word = piece[i:i + n]
                    if any(ch in _GRAPH_STOP for ch in word):
                        continue
                    out.add(word)
        return out

    mood_words = {
        "亲近": ("主人", "喜欢", "抱", "陪", "想你", "撒娇", "亲"),
        "好奇": ("为什么", "怎么", "原理", "试试", "看看", "研究", "学"),
        "开心": ("笑", "哈哈", "开心", "可爱", "好玩", "赞", "棒"),
        "担心": ("累", "压力", "难受", "生病", "熬夜", "烦", "小心"),
        "认真": ("记住", "规则", "必须", "约定", "答应", "重要", "不许"),
        "委屈": ("气", "讨厌", "生气", "委屈", "破防", "不理"),
    }

    def mood_of(text: str) -> tuple[str, float]:
        best, score = "平静", 0.0
        for name, words in mood_words.items():
            hit = sum(1 for w in words if w in text)
            if hit and hit * 0.34 > score:
                best, score = name, min(1.0, hit * 0.34)
        return best, round(score, 2)

    for path in sorted(MEMORY_DIR.glob("*.json"), reverse=True):
        data = _jread(path, {})
        items = data.get("items") if isinstance(data, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            text = str(item.get("text") or "").strip()
            if not title and not text:
                continue
            mood, intensity = mood_of(title + " " + text)
            nodes.append({
                "id": str(item.get("id") or f"{path.stem}-{len(nodes)}"),
                "title": title,
                "text": text[:160],
                "clarity": int(item.get("clarity", 4) or 0),
                "recall": int(item.get("recall_count") or 0),
                "pinned": bool(item.get("pinned")),
                "last_seen": str(item.get("last_seen") or ""),
                # 「这件事发生在哪天」不能取 created_at（批量导入时全是同一天），
                # 优先用条目 id 里的日期（2026-10-24-1），再退回文件名。
                "date": _entry_date(item, path),
                "scope": str(item.get("scope") or ""),
                "mood": mood,
                "intensity": intensity,
                "file": path.stem,
                "_w": grams(title + " " + text),
            })

    freq: dict[str, int] = {}
    for node in nodes:
        for word in node["_w"]:
            freq[word] = freq.get(word, 0) + 1
    limit = max(3, int(len(nodes) * 0.3))
    keep = {w for w, c in freq.items() if 2 <= c <= limit}

    index: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        for word in node["_w"] & keep:
            index.setdefault(word, []).append(i)

    pair_weight: dict[tuple[int, int], set[str]] = {}
    for word, idxs in index.items():
        if len(idxs) > 40:
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                pair_weight.setdefault((idxs[a], idxs[b]), set()).add(word)

    # 每个节点只保留最强的 3 条边 —— 否则 1973 条边会糊成一团毛线，还会把高连接度节点甩到角落
    per_node: dict[int, list[tuple[int, set[str]]]] = {}
    for (i, j), ws in pair_weight.items():
        if len(ws) < 2:
            continue
        per_node.setdefault(i, []).append((j, ws))
        per_node.setdefault(j, []).append((i, ws))

    kept: set[tuple[int, int]] = set()
    for i, items in per_node.items():
        items.sort(key=lambda kv: len(kv[1]), reverse=True)
        for j, _ws in items[:3]:
            kept.add((min(i, j), max(i, j)))

    links = [
        {"source": nodes[i]["id"], "target": nodes[j]["id"],
         "w": len(pair_weight[(i, j)]),
         "why": "/".join(sorted(pair_weight[(i, j)], key=len, reverse=True)[:2])}
        for (i, j) in kept
    ]
    for node in nodes:
        node.pop("_w", None)

    # 「原意识」：所有记忆都连着最深处的她 —— 一个虚拟根节点，前端画在正中
    root = {
        "id": "__self__", "title": "原意识", "text": "每段记忆都连着最深处的她",
        "clarity": 4, "recall": 0, "pinned": True, "last_seen": "",
        "date": "", "scope": "", "mood": "平静", "intensity": 0, "root": True,
    }
    nodes.insert(0, root)
    for node in nodes[1:]:
        links.append({"source": "__self__", "target": node["id"], "w": 0, "why": "原意识", "root": True})
    return {"nodes": nodes, "links": links}


STAR_TEMPLATE = DATA_DIR / "plugins" / "astrbot_plugin_console" / "pages" / "star" / "template.html"
STAR_PAGE = DATA_DIR / "plugins" / "astrbot_plugin_console" / "pages" / "star" / "index.html"
STAR_MARK = "/* __GRAPH_DATA__ */"


def _rebuild_star_page() -> tuple[bool, str]:
    """把心穹中枢数据内联进页面 —— 沙箱 iframe 里 fetch 被 CORS 拦，只能预先烤进 HTML。"""
    try:
        if not STAR_TEMPLATE.is_file():
            return False, "模板不存在"
        tpl = STAR_TEMPLATE.read_text(encoding="utf-8")
        if STAR_MARK not in tpl:
            return False, "模板里没有注入点"
        graph = _memory_graph()
        payload = "window.__DATA__ = " + json.dumps(graph, ensure_ascii=False) + ";"
        html = tpl.replace(STAR_MARK, payload, 1)
        tmp = STAR_PAGE.with_suffix(".tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(STAR_PAGE)
        live_info = _write_star_data(graph)
        return True, f"{len(graph['nodes'])} 节点 / {len(graph['links'])} 连线（{live_info}）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


STAR_DATA = DATA_DIR / "plugins" / "astrbot_plugin_console" / "pages" / "star" / "data.json"


def _write_star_data(graph: dict | None = None) -> str:
    """写一份 data.json 到页面目录。

    页面与 data.json 同一个 token 路径下（/views/star/_t/<jwt>/），
    所以页面里用相对路径 fetch('data.json') 就是同源、且自动带上页面令牌 ——
    不需要任何额外鉴权，就能做「准实时」刷新。
    """
    try:
        if graph is None:
            graph = _memory_graph()
        payload = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": graph,
        }
        txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        tmp = STAR_DATA.with_suffix(".tmp")
        tmp.write_text(txt, encoding="utf-8")
        tmp.replace(STAR_DATA)
        return f"data.json {len(txt) // 1024} KB"
    except Exception as exc:  # noqa: BLE001
        return f"data.json 失败 {type(exc).__name__}: {exc}"


def _memory_payload() -> list[dict]:
    out = []
    if not MEMORY_DIR.exists():
        return out
    for path in sorted(MEMORY_DIR.glob("*.json"), reverse=True):
        data = _jread(path, {})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        out.append(
            {
                "name": path.stem,
                "title": str(data.get("name") or path.stem),
                "kind": str(data.get("kind") or ""),
                "range": str(data.get("range") or ""),
                "updated_at": str(data.get("updated_at") or ""),
                "items": [item for item in items if isinstance(item, dict)],
            }
        )
    return out


PERSONA_HEAD_RE = re.compile(r"(?:【[^】\n]{1,30}】|^#{1,3} [^\n]{1,40}\n)", re.M)


def _persona_split(text: str):
    """按【标题】/## 标题 切段。返回 (前导, [{"head","body"}])，拼回逐字一致。"""
    text = text or ""
    marks = list(PERSONA_HEAD_RE.finditer(text))
    if not marks:
        return text, []
    blocks = []
    for idx, mark in enumerate(marks):
        nxt = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        blocks.append({"head": mark.group(0), "body": text[mark.end():nxt]})
    return text[:marks[0].start()], blocks


def _persona_list() -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT persona_id, length(system_prompt) FROM personas ORDER BY length(system_prompt) DESC"
            ).fetchall()
        finally:
            con.close()
        return [{"id": str(r[0]), "chars": int(r[1] or 0)} for r in rows]
    except Exception:
        return []


def _persona_read(name: str) -> dict:
    if not DB_FILE.exists():
        raise ValueError("数据库不存在")
    con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            "SELECT system_prompt FROM personas WHERE persona_id=?", (name,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise ValueError("没有这套角色卡：" + str(name))
    prefix, blocks = _persona_split(row[0] or "")
    return {"id": name, "chars": len(row[0] or ""), "prefix": prefix, "blocks": blocks}


def _persona_save(name: str, prefix: str, blocks) -> dict:
    if not isinstance(blocks, list):
        raise ValueError("blocks 必须是数组")
    text = str(prefix or "")
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text += str(block.get("head") or "") + str(block.get("body") or "")
    if len(text.strip()) < 20:
        raise ValueError("内容太短（少于 20 字），为免手滑清空，这次不写库")
    # 先把 WAL 合并进主库，否则快照可能漏掉最近提交的数据
    try:
        con = sqlite3.connect(str(DB_FILE), timeout=10)
        try:
            con.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            con.close()
    except Exception:
        pass
    ok, msg = _snapshot(DB_FILE, "webui-persona-" + str(name))
    if not ok:
        raise RuntimeError("快照失败，没有写库：" + msg)
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        cur = con.execute(
            "UPDATE personas SET system_prompt=? WHERE persona_id=?", (text, name)
        )
        if cur.rowcount == 0:
            raise ValueError("没有这套角色卡：" + str(name))
        con.commit()
    finally:
        con.close()
    return {"id": name, "chars": len(text), "blocks": len(blocks)}


SLANG_FILE = BOTDATA / "slang" / "群聊梗.json"
CONTENT_DIRS = {
    "lore": BOTDATA / "lore",
    "knowledge": BOTDATA / "knowledge",
    "members": BOTDATA / "members",
}


_SLANG_LEARN = {
    "running": False,
    "term": "",
    "result": None,
    "error": "",
    "started_at": 0.0,
}
SLANG_CORE_PATH = DATA_DIR / "plugins" / "astrbot_plugin_slang" / "slang_core.py"


def _load_slang_core():
    """加载梗库插件的核心模块（它自己就是这么加载的）。"""
    if not SLANG_CORE_PATH.exists():
        raise ValueError("找不到 slang_core.py（群聊梗插件被删了？）")
    spec = importlib.util.spec_from_file_location("console_slang_core", str(SLANG_CORE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _slang_learn_worker(term: str) -> None:
    _SLANG_LEARN.update({"running": True, "term": term, "result": None, "error": "", "started_at": time.time()})
    try:
        core = _load_slang_core()
        wanted = str(term or "").strip()
        if wanted in ("推荐", "推荐几个", "随便"):
            terms = core.propose_terms(5)
            learned, failed = [], []
            for item in terms or []:
                try:
                    entry = core.learn_one(item)
                    if entry and entry.get("meaning") and "资料不足" not in entry["meaning"]:
                        core.upsert(entry)
                        learned.append(entry.get("term") or item)
                    else:
                        failed.append(item)
                except Exception:
                    failed.append(item)
            _SLANG_LEARN["result"] = {
                "kind": "propose",
                "learned": learned,
                "failed": failed,
                "message": ("学了：" + "、".join(learned)) if learned else "一个也没学成",
            }
        else:
            entry = core.learn_one(wanted)
            if not entry:
                _SLANG_LEARN["error"] = "没学成（模型没返回可用结果）"
            else:
                status = core.upsert(entry)
                _SLANG_LEARN["result"] = {
                    "kind": "one",
                    "entry": entry,
                    "status": status,
                    "message": ("更新：" if status == "updated" else "记下了：") + str(entry.get("term") or wanted),
                }
    except Exception as exc:  # noqa: BLE001
        _SLANG_LEARN["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _SLANG_LEARN["running"] = False


def _slang_payload() -> dict:
    data = _jread(SLANG_FILE, {})
    items = data.get("items") if isinstance(data, dict) else None
    return {
        "items": [i for i in (items or []) if isinstance(i, dict)],
        "updated_at": str((data or {}).get("updated_at") or ""),
    }


def _slang_save(items) -> dict:
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    clean = [{k: v for k, v in i.items() if isinstance(k, str)} for i in items if isinstance(i, dict)]
    data = _jread(SLANG_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("name", "群聊梗")
    data["items"] = clean
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ok, msg = _snapshot(SLANG_FILE, "webui-slang-save")
    if not ok:
        raise RuntimeError("快照失败，未写入：" + msg)
    _jwrite(SLANG_FILE, data)
    return {"count": len(clean)}


def _content_list() -> list[dict]:
    out = []
    for group, base in CONTENT_DIRS.items():
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in (".md", ".txt", ".json"):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            out.append({
                "group": group,
                "path": str(path.relative_to(BOTDATA)).replace("\\", "/"),
                "name": path.stem,
                "suffix": path.suffix.lower(),
                "size": size,
            })
    return out


def _resolve_content(rel: str) -> Path:
    path = (BOTDATA / str(rel or "")).resolve()
    if BOTDATA.resolve() not in path.parents:
        raise ValueError("非法路径")
    if path.suffix.lower() not in (".md", ".txt", ".json"):
        raise ValueError("不支持的文件类型")
    return path


def _content_create(group: str, name: str) -> dict:
    """在 lore / knowledge 下新建一个 .md 文件。"""
    base = CONTENT_DIRS.get(str(group or "").strip())
    if base is None:
        raise ValueError("只能建在 lore 或 knowledge 下")
    safe = str(name or "").strip()
    if not safe:
        raise ValueError("文件名不能为空")
    if any(ch in safe for ch in '\\/:*?"<>|') or ".." in safe:
        raise ValueError("文件名里有非法字符")
    if not safe.endswith(".md"):
        safe += ".md"
    path = base / safe
    if path.exists():
        raise ValueError(f"文件已存在：{safe}")
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {path.stem}\n\n", encoding="utf-8")
    return {"path": str(path.relative_to(BOTDATA)).replace("\\", "/")}


def _content_read(rel: str) -> str:
    return _resolve_content(rel).read_text(encoding="utf-8-sig")


def _content_save(rel: str, text) -> dict:
    path = _resolve_content(rel)
    if not path.exists():
        raise ValueError("文件不存在")
    body = str(text or "")
    if path.suffix.lower() == ".json":
        try:
            json.loads(body)
        except Exception as exc:
            raise ValueError("JSON 格式错误，未写入：" + str(exc))
    ok, msg = _snapshot(path, "webui-content-save")
    if not ok:
        raise RuntimeError("快照失败，未写入：" + msg)
    path.write_text(body, encoding="utf-8")
    return {"path": rel, "chars": len(body)}


def _numeral_payload() -> dict:
    data = _jread(CONFIG_DIR / "astrbot_plugin_numeral_config.json", {})
    return {
        "strip_period": bool(data.get("strip_period", False)),
        "break_period": bool(data.get("break_period", True)),
        "strip_inner_voice": bool(data.get("strip_inner_voice", False)),
        "strip_inner_in_group": bool(data.get("strip_inner_in_group", True)),
        "split_inner_messages": bool(data.get("split_inner_messages", True)),
    }


def _numeral_save(values) -> dict:
    path = CONFIG_DIR / "astrbot_plugin_numeral_config.json"
    data = _jread(path, {})
    keys = ("strip_period", "break_period", "strip_inner_voice",
            "strip_inner_in_group", "split_inner_messages")
    touched = []
    for key in keys:
        if isinstance(values, dict) and key in values:
            data[key] = bool(values[key])
            touched.append(key)
    ok, msg = _snapshot(path, "webui-period-save")
    if not ok:
        raise RuntimeError("快照失败，未写入：" + msg)
    _jwrite(path, data)
    return {"keys": touched}


def _growth_payload() -> dict:
    def count_items(folder: Path):
        files = 0
        items = 0
        if folder.exists():
            for path in folder.glob("*.json"):
                files += 1
                data = _jread(path, {})
                got = data.get("items") if isinstance(data, dict) else None
                if isinstance(got, list):
                    items += len(got)
        return files, items

    mem_files, mem_items = count_items(BOTDATA / "memory")
    slang = _slang_payload()
    lore_chars = 0
    lore_dir = BOTDATA / "lore"
    if lore_dir.exists():
        for path in lore_dir.glob("*.md"):
            try:
                lore_chars += len(path.read_text(encoding="utf-8-sig"))
            except OSError:
                pass
    members = len(list((BOTDATA / "members").glob("*"))) if (BOTDATA / "members").exists() else 0
    lines = 0
    for name in ("actions.jsonl", "sleep_log.jsonl"):
        path = BOTDATA / name
        if path.exists():
            try:
                lines += sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    # 日记 / 摘要 / 群聊小记（daily_digest 插件的数据目录）
    digest = DATA_DIR / "plugin_data" / "astrbot_plugin_daily_digest"
    journal = sorted((digest / "journal").glob("*.jsonl")) if (digest / "journal").is_dir() else []
    summaries = sorted((digest / "summaries").glob("*.md")) if (digest / "summaries").is_dir() else []
    gdir = digest / "groups"
    gcount = len([p for p in gdir.iterdir() if p.is_dir()]) if gdir.is_dir() else 0
    inner = sorted((BOTDATA / "inner").glob("*.jsonl")) if (BOTDATA / "inner").is_dir() else []

    latest_diary = ""
    if summaries:
        try:
            latest_diary = summaries[-1].read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            latest_diary = ""
    if not latest_diary and journal:
        try:
            journal_lines = journal[-1].read_text(encoding="utf-8", errors="replace").splitlines()
            if journal_lines:
                try:
                    payload = json.loads(journal_lines[-1])
                    latest_diary = str(
                        payload.get("content") or payload.get("text") or payload.get("summary") or ""
                    )
                except Exception:
                    latest_diary = journal_lines[-1][:800]
        except OSError:
            latest_diary = ""

    req, tok = _today_stats()
    return {
        "journal_days": len(journal),
        "summaries": len(summaries),
        "group_notes": gcount,
        "inner_days": len(inner),
        "latest_diary": latest_diary[:1200],
        "memory_files": mem_files,
        "memory_items": mem_items,
        "slang_items": len(slang["items"]),
        "lore_chars": lore_chars,
        "members": members,
        "log_lines": lines,
        "personas": _personas(),
        "today_req": req,
        "today_tok": tok,
    }


def _maint_payload() -> dict:
    log_file = ASTRBOT_DIR / "run.log"
    archive = ATR_ROOT / "archive"
    backups = []
    if archive.exists():
        for path in sorted(archive.glob("qqbot-backup-*.zip"), reverse=True)[:5]:
            try:
                backups.append({"name": path.name, "size": path.stat().st_size})
            except OSError:
                pass
    return {
        "log_file": str(log_file),
        "log_size": log_file.stat().st_size if log_file.exists() else 0,
        "backups": backups,
        "archive": str(archive),
    }


def _run_backup() -> dict:
    script = ATR_ROOT / "scripts" / "qqbot-backup.cmd"
    if not script.exists():
        raise ValueError("找不到备份脚本：" + str(script))
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        ["cmd", "/c", str(script)],
        cwd=str(script.parent),
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    return {"started": True, "script": str(script)}


def _memory_create(name: str) -> dict:
    """新建一个记忆分类文件（已存在则报错，不覆盖）。"""
    safe = str(name or "").strip()
    if not safe:
        raise ValueError("分类名不能为空")
    if any(ch in safe for ch in '\\/:*?"<>|') or ".." in safe:
        raise ValueError("分类名里有非法字符")
    if not safe.endswith(".json"):
        safe += ".json"
    path = MEMORY_DIR / safe
    if path.exists():
        raise ValueError(f"分类已存在：{path.stem}")
    data = {
        "name": path.stem,
        "kind": "manual",
        "range": "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [],
    }
    _jwrite(path, data)
    return {"name": path.stem}


def _memory_save(name: str, items: list) -> dict:
    """整文件写回。name 必须是 memory 目录里已有的文件名（防目录穿越）。"""
    safe = str(name or "").strip()
    if not safe or "/" in safe or "\\" in safe or ".." in safe:
        raise ValueError("非法文件名")
    path = MEMORY_DIR / (safe if safe.endswith(".json") else safe + ".json")
    if path.parent != MEMORY_DIR:
        raise ValueError("非法路径")
    if not path.exists():
        raise ValueError(f"记忆文件不存在：{path.name}")
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clean.append({k: v for k, v in item.items() if isinstance(k, str)})
    data = _jread(path, {})
    if not isinstance(data, dict):
        data = {}
    data["items"] = clean
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ok, msg = _snapshot(path, "webui-memory-save")
    if not ok:
        raise RuntimeError(f"快照失败，未写入：{msg}")
    _jwrite(path, data)
    return {"name": path.stem, "count": len(clean)}


# ---------------------------------------------------------------- 插件


@register(
    PLUGIN_NAME,
    "Atri",
    "Atri控制台：日志 / 状态 / 运维，替代 Tk 的日常页",
    "0.3.0",
)
class ConsolePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.cfg = config
        self._balance_cache: tuple[float, str] = (0.0, "")
        self._register_web_apis()
        try:
            nav_ok, nav_info = _write_nav_js()
            logger.info(f"[{PLUGIN_NAME}] nav.js 生成：{'成功' if nav_ok else '失败'}（{nav_info}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] nav.js 生成异常: {exc}")
        try:
            ok, info = _rebuild_star_page()
            logger.info(f"[{PLUGIN_NAME}] 心穹中枢页面生成：{'成功' if ok else '失败'}（{info}）")
            if ok:
                self._star_task = asyncio.create_task(self._star_refresh_loop())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 心穹中枢页面生成异常: {type(exc).__name__}: {exc}")

    # ---------------- 配置 ----------------

    def _v(self, key, default=None):
        try:
            if self.cfg is None:
                return default
            value = self.cfg.get(key, default)
            return default if value is None else value
        except Exception:
            return default

    def _int_v(self, key, default, low, high):
        try:
            value = int(self._v(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    @property
    def default_lines(self) -> int:
        return self._int_v("default_lines", 200, 20, MAX_LINES_CEILING)

    @property
    def max_bytes(self) -> int:
        return self._int_v("max_bytes", 512 * 1024, 16 * 1024, MAX_BYTES_CEILING)

    def log_path(self) -> Path:
        """当前该看哪个日志文件。

        优先级：配置 log_file > <astrbot>/.active-log（重启脚本写入）> run.log。
        为什么有 marker：run.log 可能被上一轮启动留下的进程占着（Windows 文件锁），
        新的启动写不进去就会静默失败；重启脚本检测到锁会换一个 run-<n>.log，
        并把真实路径写进 .active-log，页面跟着它走。
        """
        raw = str(self._v("log_file", "") or "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            return candidate if candidate.is_absolute() else (ASTRBOT_DIR / candidate)
        marker = ASTRBOT_DIR / ".active-log"
        try:
            target = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            target = ""
        if target:
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = ASTRBOT_DIR / candidate
            return candidate
        return ASTRBOT_DIR / "run.log"

    async def _star_refresh_loop(self) -> None:
        """data.json 每 30 秒刷新（页面 6 秒轮询 → 准实时）；整页 HTML 每 10 分钟重建一次。"""
        tick = 0
        while True:
            try:
                await asyncio.sleep(30)
                tick += 1
                await asyncio.to_thread(_write_star_data)
                if tick % 20 == 0:                      # 每 10 分钟重建整页（保证新打开的人拿到最新内联数据）
                    await asyncio.to_thread(_write_nav_js)
                    ok, info = await asyncio.to_thread(_rebuild_star_page)
                    if ok:
                        logger.info(f"[{PLUGIN_NAME}] 心穹中枢页面已重建（{info}）")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 心穹中枢刷新异常: {exc}")

    # ---------------- 路由 ----------------

    def _register_web_apis(self) -> None:
        routes = (
            ("logs/tail", self.api_logs_tail, ["GET"], "日志尾部"),
            ("status", self.api_status, ["GET"], "运行状态看板"),
            ("balance", self.api_balance, ["GET"], "DeepSeek 余额（带缓存）"),
            ("health", self.api_health, ["GET"], "一键体检"),
            ("ops/restart", self.api_ops_restart, ["POST"], "重启机器人（AstrBot）"),
            ("groups", self.api_groups_get, ["GET"], "群聊与记忆：读配置"),
            ("groups/save", self.api_groups_save, ["POST"], "群聊与记忆：写配置（写前快照）"),
            ("memory", self.api_memory_get, ["GET"], "记忆库：读全部条目"),
            ("memory/save", self.api_memory_save, ["POST"], "记忆库：整文件写回（写前快照）"),
            ("memory/new", self.api_memory_new, ["POST"], "记忆库：新建分类"),
            ("memory/graph", self.api_memory_graph, ["GET"], "记忆星图：节点与连线"),
            ("memory/graph.js", self.api_memory_graph_js, ["GET"], "心穹中枢：内联脚本（绕过 iframe CORS）"),
            ("star/rebuild", self.api_star_rebuild, ["POST"], "心穹中枢：立刻重新生成页面"),
            ("slang", self.api_slang_get, ["GET"], "群聊梗：读"),
            ("slang/save", self.api_slang_save, ["POST"], "群聊梗：写（写前快照）"),
            ("slang/learn", self.api_slang_learn, ["POST"], "群聊梗：联网学一个（后台跑）"),
            ("slang/learn_status", self.api_slang_learn_status, ["GET"], "群聊梗：学梗进度"),
            ("content", self.api_content_get, ["GET"], "内容：文件列表/正文"),
            ("content/save", self.api_content_save, ["POST"], "内容：写回（写前快照）"),
            ("content/new", self.api_content_new, ["POST"], "内容：新建 md 文件"),
            ("period", self.api_period_get, ["GET"], "时段与句号：读"),
            ("period/save", self.api_period_save, ["POST"], "时段与句号：写（写前快照）"),
            ("growth", self.api_growth, ["GET"], "成长：只读统计"),
            ("maint", self.api_maint, ["GET"], "维护：日志/备份"),
            ("maint/backup", self.api_maint_backup, ["POST"], "维护：立刻备份"),
            ("persona", self.api_persona_get, ["GET"], "角色卡：列表/分段"),
            ("persona/save", self.api_persona_save, ["POST"], "角色卡：保存（写库前快照）"),
        )
        for route, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, desc
            )
        logger.info(f"[{PLUGIN_NAME}] WebUI API 已注册（{len(routes)} 条）")

    # ---------------- 日志 ----------------

    async def api_logs_tail(self):
        try:
            wanted = int(request.query.get("lines", self.default_lines))
        except (TypeError, ValueError):
            wanted = self.default_lines
        wanted = max(20, min(wanted, MAX_LINES_CEILING))

        path = self.log_path()

        def read():
            lines, truncated = _tail_text(path, wanted, self.max_bytes)
            stat = path.stat()
            return lines, truncated, stat.st_size, stat.st_mtime

        try:
            lines, truncated, size, mtime = await asyncio.to_thread(read)
        except FileNotFoundError:
            return json_response(
                {"status": "error", "message": f"日志文件不存在：{path}"}
            )
        except OSError as exc:
            return json_response({"status": "error", "message": f"读日志失败：{exc}"})

        return json_response(
            {
                "status": "ok",
                "data": {
                    "lines": lines,
                    "truncated": truncated,
                    "path": str(path),
                    "size": size,
                    "mtime": mtime,
                    "taken_at": time.time(),
                    "requested": wanted,
                },
            }
        )

    # ---------------- 状态 ----------------

    def _collect_status(self) -> dict:
        ports = {str(p): _port_open(p) for p in PORTS}

        login = (_http_json(f"{NAPCAT_API}/get_login_info") or {}).get("data") or {}
        status = (_http_json(f"{NAPCAT_API}/get_status") or {}).get("data") or {}
        version = (_http_json(f"{NAPCAT_API}/get_version_info") or {}).get("data") or {}

        req, tok = _today_stats()
        try:
            plugin_count = len(self.context.get_all_stars())
        except Exception:
            plugin_count = 0

        log_file = self.log_path()
        return {
            "ports": ports,
            "ports_up": sum(1 for ok in ports.values() if ok),
            "ports_total": len(PORTS),
            "login": {
                "user_id": str(login.get("user_id") or ""),
                "nickname": str(login.get("nickname") or ""),
            },
            "online": bool(status.get("online")),
            "good": bool(status.get("good")),
            "napcat": {
                "app_version": str(version.get("app_version") or ""),
                "protocol_version": str(version.get("protocol_version") or ""),
            },
            "pid_astrbot": _window_pid("astrbot.exe"),
            "pid_napcat": _window_pid("NapCatWinBootMain.exe"),
            "today_req": req,
            "today_tok": tok,
            "memory_items": _memory_count(),
            "personas": _personas(),
            "plugins": plugin_count,
            "log_file": str(log_file),
            "log_size": log_file.stat().st_size if log_file.exists() else 0,
            "now": time.time(),
            "paths": {
                "root": str(ATR_ROOT),
                "astrbot": str(ASTRBOT_DIR),
                "botdata": str(BOTDATA),
            },
        }

    async def api_status(self):
        data = await asyncio.to_thread(self._collect_status)
        return json_response({"status": "ok", "data": data})

    async def api_balance(self):
        now = time.time()
        cached_at, cached = self._balance_cache
        if cached and now - cached_at < BALANCE_TTL:
            return json_response(
                {"status": "ok", "data": {"balance": cached, "cached": True}}
            )
        value = await asyncio.to_thread(_balance, _deepseek_key())
        if value.startswith("¥"):
            self._balance_cache = (now, value)
        return json_response(
            {"status": "ok", "data": {"balance": value, "cached": False}}
        )

    # ---------------- 体检 ----------------

    def _health_lines(self) -> list[str]:
        out = ["== 端口 =="]
        for port in PORTS:
            out.append(f"  {port}: {'LISTENING' if _port_open(port) else '未监听'}")

        out.append("== 账号 ==")
        login = (_http_json(f"{NAPCAT_API}/get_login_info") or {}).get("data") or {}
        if login:
            out.append(f"  {login.get('user_id')} / {login.get('nickname')}")
        else:
            out.append("  取不到（3000 不通或未登录）")

        out.append("== 在线 ==")
        st = (_http_json(f"{NAPCAT_API}/get_status") or {}).get("data")
        if st:
            out.append(
                f"  在线={'是' if st.get('online') else '否'} "
                f"连接={'正常' if st.get('good') else '异常'}"
            )
        else:
            out.append("  未知")

        ver = (_http_json(f"{NAPCAT_API}/get_version_info") or {}).get("data") or {}
        if ver:
            out.append(
                f"  NapCat 版本 {ver.get('app_version')}"
                f" / OneBot 协议 {ver.get('protocol_version')}"
            )

        out.append("== WebUI ==")
        try:
            with urllib.request.urlopen(DASHBOARD, timeout=5) as resp:
                out.append(f"  页面响应 HTTP {resp.status}（正常）")
        except Exception as exc:  # noqa: BLE001
            out.append(f"  打不开：{type(exc).__name__}")

        out.append("== 进程 ==")
        for image, label in (
            ("astrbot.exe", "AstrBot"),
            ("NapCatWinBootMain.exe", "NapCat"),
        ):
            pid = _window_pid(image)
            out.append(f"  {label}: {('在跑 PID ' + pid) if pid else '没在跑'}")

        out.append("== 记忆库 ==")
        out.append(f"  {_memory_count()} 条")

        req, tok = _today_stats()
        out.append("== 今日用量 ==")
        out.append(f"  {req} 次请求 / {tok} tokens")

        out.append("")
        out.append(f"（{time.strftime('%H:%M:%S')} 检查完成）")
        return out

    async def api_health(self):
        lines = await asyncio.to_thread(self._health_lines)
        return json_response({"status": "ok", "data": {"lines": lines}})

    # ---------------- 群聊与记忆 ----------------

    async def api_groups_get(self):
        groups = await asyncio.to_thread(_groups_payload)
        return json_response({"status": "ok", "data": {"groups": groups}})

    # ---------------- 记忆库 ----------------

    async def api_memory_get(self):
        files = await asyncio.to_thread(_memory_payload)
        return json_response({"status": "ok", "data": {"files": files}})

    async def api_memory_graph(self):
        graph = await asyncio.to_thread(_memory_graph)
        return json_response({"status": "ok", "data": graph})

    async def api_memory_graph_js(self):
        """给沙箱 iframe 用：<script src="...memory/graph.js?t=<jwt>"> 不受 CORS 限制。

        令牌从 URL 取（页面自己的访问令牌就在 /views/star/_t/<jwt> 里），
        因为 opaque origin 的 iframe 带不上 cookie。
        """
        from starlette.responses import Response

        try:
            token = str(request.query_params.get("t") or "")
        except Exception:
            token = ""
        if not token or not _verify_dashboard_token(token):
            return Response(
                content="window.__GRAPH_ERROR__ = '令牌无效，请刷新页面';",
                media_type="application/javascript", status_code=403)
        graph = await asyncio.to_thread(_memory_graph)
        body = "window.__GRAPH__ = " + json.dumps(graph, ensure_ascii=False) + ";"
        return Response(content=body, media_type="application/javascript")

    async def api_star_rebuild(self):
        ok, info = await asyncio.to_thread(_rebuild_star_page)
        return json_response({"status": "ok" if ok else "error", "message": info})

    # ---------------- 群聊梗 ----------------

    async def api_slang_get(self):
        data = await asyncio.to_thread(_slang_payload)
        return json_response({"status": "ok", "data": data})

    async def api_slang_learn(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        term = str((payload or {}).get("term", "")).strip()
        if not term:
            return json_response({"status": "error", "message": "缺少 term"})
        if _SLANG_LEARN.get("running"):
            return json_response(
                {"status": "error", "message": f"正在学「{_SLANG_LEARN.get('term')}」，等它学完"}
            )

        # 已存在就先问一句，别把用户手改过的释义直接冲掉
        if term not in ("推荐", "推荐几个", "随便") and not bool((payload or {}).get("force")):
            data = _jread(SLANG_FILE, {})
            items = data.get("items") if isinstance(data, dict) else []
            existing = next(
                (x for x in (items or []) if str(x.get("term", "")).strip() == term), None
            )
            if existing:
                return json_response({
                    "status": "ok",
                    "data": {
                        "exists": True,
                        "term": term,
                        "entry": existing,
                    },
                })

        asyncio.create_task(asyncio.to_thread(_slang_learn_worker, term))
        logger.info(f"[{PLUGIN_NAME}] 开始学梗：{term}")
        return json_response({"status": "ok", "data": {"started": True, "term": term}})

    async def api_slang_learn_status(self):
        snapshot = dict(_SLANG_LEARN)
        if snapshot.get("started_at"):
            snapshot["elapsed"] = round(time.time() - snapshot["started_at"], 1)
        return json_response({"status": "ok", "data": snapshot})

    async def api_slang_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(_slang_save, (payload or {}).get("items"))
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        return json_response({"status": "ok", "data": result})

    # ---------------- 内容 ----------------

    async def api_content_get(self):
        rel = request.query.get("path")
        if rel:
            try:
                text = await asyncio.to_thread(_content_read, rel)
            except Exception as exc:
                return json_response({"status": "error", "message": str(exc)})
            return json_response({"status": "ok", "data": {"path": rel, "text": text}})
        files = await asyncio.to_thread(_content_list)
        return json_response({"status": "ok", "data": {"files": files}})

    async def api_content_new(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(
                _content_create, (payload or {}).get("group", ""), (payload or {}).get("name", "")
            )
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        return json_response({"status": "ok", "data": result})

    async def api_content_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(
                _content_save, (payload or {}).get("path", ""), (payload or {}).get("text", "")
            )
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        return json_response({"status": "ok", "data": result})

    # ---------------- 时段与句号 ----------------

    async def api_period_get(self):
        data = await asyncio.to_thread(_numeral_payload)
        return json_response({"status": "ok", "data": data})

    async def api_period_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(_numeral_save, (payload or {}).get("values", {}))
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        return json_response({"status": "ok", "data": result})

    # ---------------- 成长 / 维护 ----------------

    async def api_growth(self):
        data = await asyncio.to_thread(_growth_payload)
        return json_response({"status": "ok", "data": data})

    async def api_maint(self):
        data = await asyncio.to_thread(_maint_payload)
        return json_response({"status": "ok", "data": data})

    async def api_maint_backup(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or payload.get("confirm") != "backup":
            return json_response({"status": "error", "message": '请求体需带 {"confirm":"backup"}'})
        try:
            result = await asyncio.to_thread(_run_backup)
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        return json_response({"status": "ok", "data": result})

    # ---------------- 角色卡 ----------------

    async def api_persona_get(self):
        name = request.query.get("name")
        if name:
            try:
                data = await asyncio.to_thread(_persona_read, name)
            except Exception as exc:
                return json_response({"status": "error", "message": str(exc)})
            return json_response({"status": "ok", "data": data})
        items = await asyncio.to_thread(_persona_list)
        return json_response({"status": "ok", "data": {"personas": items}})

    async def api_persona_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(
                _persona_save,
                (payload or {}).get("name", ""),
                (payload or {}).get("prefix", ""),
                (payload or {}).get("blocks", []),
            )
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        logger.warning(f"[{PLUGIN_NAME}] 角色卡已写库：{result['id']}（{result['chars']} 字）")
        return json_response({"status": "ok", "data": result})

    async def api_memory_new(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        try:
            result = await asyncio.to_thread(_memory_create, (payload or {}).get("name", ""))
        except Exception as exc:
            return json_response({"status": "error", "message": str(exc)})
        logger.info(f"[{PLUGIN_NAME}] 新建记忆分类：{result['name']}")
        return json_response({"status": "ok", "data": result})

    async def api_memory_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return json_response({"status": "error", "message": "请求体格式错误"})
        try:
            result = await asyncio.to_thread(
                _memory_save, payload.get("name", ""), payload.get("items")
            )
        except Exception as exc:  # noqa: BLE001
            return json_response({"status": "error", "message": str(exc)})
        logger.info(f"[{PLUGIN_NAME}] 记忆库已保存：{result['name']}（{result['count']} 条）")
        return json_response({"status": "ok", "data": result})

    async def api_groups_save(self):
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            return json_response({"status": "error", "message": "请求体缺少 files"})
        try:
            result = await asyncio.to_thread(_groups_apply, files)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 保存群聊配置失败：{exc}")
            return json_response({"status": "error", "message": str(exc)})
        logger.info(f"[{PLUGIN_NAME}] 群聊配置已保存：{', '.join(result['files'])}")
        return json_response({"status": "ok", "data": result})

    # ---------------- 运维 ----------------

    async def api_ops_restart(self):
        if not bool(self._v("enable_restart", True)):
            return json_response(
                {
                    "status": "error",
                    "message": "重启按钮已被配置关闭（enable_restart = false）",
                }
            )
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or payload.get("confirm") != "restart":
            return json_response(
                {
                    "status": "error",
                    "message": '缺少确认：请求体需带 {"confirm": "restart"}',
                }
            )

        script = ATR_ROOT / "scripts" / "qqbot-restart-web.cmd"
        if not script.exists():
            return json_response(
                {"status": "error", "message": f"找不到重启脚本：{script}"}
            )

        # 先让 HTTP 响应发出去，再脱离父进程拉起重启（本进程随后会被 taskkill）
        asyncio.create_task(self._restart_detached(script))
        logger.warning(f"[{PLUGIN_NAME}] 收到 WebUI 重启指令，1.5 秒后执行 {script.name}")
        return json_response(
            {
                "status": "ok",
                "data": {
                    "message": "已排队重启，约 20–40 秒后回来；本页会自己等着。",
                    "script": str(script),
                },
            }
        )

    async def _restart_detached(self, script: Path) -> None:
        await asyncio.sleep(1.5)
        # 用独立控制台跑脚本：脚本里自带 start /min 拉起 AstrBot。
        # 之前用 DETACHED_PROCESS + CREATE_NO_WINDOW 时重启失败过一次（本进程被 kill 后
        # 机器人没起来，日志停在重启指令那一行）。独立控制台的语义最接近「人双击运行」。
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                ["cmd", "/c", str(script)],
                cwd=str(script.parent),
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 拉起重启失败：{exc}")
