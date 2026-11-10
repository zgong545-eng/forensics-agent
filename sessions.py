# -*- coding: utf-8 -*-
"""
会话存储（历史对话）—— 列表 / 读取 / 重命名 / 归档 / 删除。

设计要点
--------
1. **沿用已有的落盘格式**：`logs/<id>.jsonl` 就是一次会话的全程留痕，
   不另建数据库，用户随时能拿文本编辑器打开看。
2. **元数据旁挂**：标题、置顶、归档标记写在 `logs/<id>.meta.json`，
   丢了也能从 jsonl 重新推出来（标题 = 第一条 user 消息）。
3. **归档 = 打标记**，不挪文件。挪文件要删源文件，在带回收站钩子 /
   文件被占用 / 跨盘的环境里会失败，还会留下「两边都有一份」的脏状态。
4. **零依赖**：标准库 json / os / pathlib。

从 jsonl 重建出来的对话只有「问 / 答」文本，工具调用作为附件挂在答案下面 ——
因为日志里没记 tool_call_id，还原不出模型要的 tool 消息对。
续聊时给模型的就是这些问答文本，工具过程留在界面上给人看。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_BYTES = 8 * 1024 * 1024      # 单个会话最多解析 8MB，防手滑塞了个大文件进来


def _safe_id(sid: str) -> str:
    sid = (sid or "").strip()
    if not sid or not _ID_RE.match(sid):
        raise ValueError("会话 id 不合法")
    return sid


def _ts_of(rec: dict) -> str:
    return str(rec.get("ts") or "")


def _pretty_time(iso: str) -> str:
    """2026-10-29T15:28:01 → 2026-10-29 15:28"""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso.replace("T", " ")[:16]


def _fallback_title(info: dict) -> str:
    """没提过问的会话给个能看懂的名字，别一律「（空会话）」。"""
    if info.get("tools"):
        return f"手动调用 · {info['tools']} 个工具"
    return "（空会话）"


class SessionStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---------------- 路径 ----------------
    def _find(self, sid: str) -> tuple[Path, Path]:
        """返回 (jsonl 路径, meta 路径)。"""
        sid = _safe_id(sid)
        p = self.root / (sid + ".jsonl")
        if not p.is_file():
            raise FileNotFoundError(f"会话不存在：{sid}")
        return p, (self.root / (sid + ".meta.json"))

    def path_of(self, sid: str) -> Path:
        """会话文件的绝对路径（续聊时要把 Session 指到同一个文件上）。"""
        return self._find(sid)[0]

    # ---------------- 元数据 ----------------
    def read_meta(self, sid: str) -> dict:
        try:
            _, mp = self._find(sid)
        except FileNotFoundError:
            return {}
        if mp.is_file():
            try:
                return json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def write_meta(self, sid: str, **kv) -> dict:
        sid = _safe_id(sid)
        _, mp = self._find(sid)
        meta = self.read_meta(sid)
        meta.update({k: v for k, v in kv.items() if v is not None})
        tmp = mp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, mp)
        return meta

    def _is_archived(self, sid: str) -> bool:
        return bool(self.read_meta(sid).get("archived"))

    # ---------------- 解析 ----------------
    def _scan(self, path: Path) -> dict:
        """扫一遍 jsonl，抽出标题、轮数、工具数、时间范围、最后一句答复。"""
        info = {"turns": 0, "tools": 0, "blocked": 0, "title": "",
                "first_ts": "", "last_ts": "", "preview": "", "msgs": 0}
        if path.stat().st_size > _MAX_BYTES:
            info["preview"] = "（会话过大，仅列概要）"
            return info
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                ts = _ts_of(rec)
                if ts:
                    info["first_ts"] = info["first_ts"] or ts
                    info["last_ts"] = ts
                if kind == "user":
                    info["turns"] += 1
                    info["msgs"] += 1
                    if not info["title"]:
                        info["title"] = re.sub(r"\s+", " ", str(rec.get("text") or "")).strip()
                elif kind == "tool_call":
                    info["tools"] += 1
                elif kind == "tool_blocked":
                    info["blocked"] += 1
                elif kind == "assistant_final":
                    info["msgs"] += 1
                    # 预览只取正文段落：表格行/分隔线拼进来会变成一串乱码似的碎片
                    keep = []
                    for line in str(rec.get("text") or "").splitlines():
                        s = line.strip()
                        if not s or s.startswith("|") or set(s) <= set("-: "):
                            continue
                        keep.append(s)
                    txt = re.sub(r"[#>*`]+", " ", " ".join(keep))
                    info["preview"] = re.sub(r"\s+", " ", txt).strip()[:120]
        return info

    def list(self, archived: bool = False) -> list[dict]:
        if not self.root.is_dir():
            return []
        rows = []
        for p in self.root.glob("*.jsonl"):
            sid = p.stem
            if not _ID_RE.match(sid):
                continue
            try:
                meta = self.read_meta(sid)
                info = self._scan(p)
            except Exception:
                continue
            if bool(meta.get("archived")) != bool(archived):
                continue
            title = (meta.get("title") or "").strip() or info["title"] or _fallback_title(info)
            rows.append({
                "id": sid,
                "title": title[:60],
                "pinned": bool(meta.get("pinned")),
                "archived": archived,
                "turns": info["turns"],
                "tools": info["tools"] + info["blocked"],
                "blocked": info["blocked"],
                "msgs": info["msgs"],
                "size": p.stat().st_size,
                "started": info["first_ts"],
                "updated": info["last_ts"] or info["first_ts"],
                "startedText": _pretty_time(info["first_ts"]),
                "updatedText": _pretty_time(info["last_ts"] or info["first_ts"]),
                "preview": info["preview"],
                "named": bool((meta.get("title") or "").strip()),
                # 一句话都没说过、也没调过工具 = 噪音（早期每次起服务都会留一个）
                "empty": not info["turns"] and not info["tools"] and not info["msgs"],
            })
        # 置顶优先，然后按最后活动时间倒序
        rows.sort(key=lambda r: (r["pinned"], r["updated"] or ""), reverse=True)
        return rows

    def read(self, sid: str) -> dict:
        """重建对话：turn = {role, text, ts, tools[]}。"""
        p, _ = self._find(sid)
        meta = self.read_meta(sid)
        turns: list[dict] = []
        cur: dict | None = None
        pending_tools: list[dict] = []

        def flush_assistant():
            nonlocal cur, pending_tools
            if cur is not None and (cur.get("text") or pending_tools):
                cur["tools"] = pending_tools
                turns.append(cur)
            cur, pending_tools = None, []

        if p.stat().st_size > _MAX_BYTES:
            raise ValueError("会话文件过大（> 8MB），请在 logs 目录里直接查看")
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                kind = rec.get("kind")
                if kind == "user":
                    flush_assistant()
                    cur = {"role": "user", "text": str(rec.get("text") or ""), "ts": _ts_of(rec)}
                    turns.append(cur)
                    cur = None
                elif kind == "tool_call":
                    pending_tools.append({
                        "tool": rec.get("tool") or "",
                        "args": rec.get("args") or "",
                        "elapsed": rec.get("elapsed"),
                        "result": str(rec.get("result") or "")[:8000],
                        "blocked": False,
                    })
                elif kind == "tool_blocked":
                    pending_tools.append({
                        "tool": rec.get("tool") or "",
                        "args": rec.get("args") or "",
                        "elapsed": None,
                        "result": "",
                        "blocked": True,
                    })
                elif kind == "assistant_final":
                    cur = {"role": "assistant", "text": str(rec.get("text") or ""), "ts": _ts_of(rec)}
                    flush_assistant()
        flush_assistant()
        # 老会话（落盘缺陷修好之前留下的）只有提问、没有回答。
        # 这里如实标一下，别让用户以为「答案又丢了」——答案当时压根没写进去。
        if turns and turns[-1].get("role") == "user":
            turns[-1]["missingAnswer"] = True
        info = self._scan(p)
        return {
            "id": sid,
            "title": (meta.get("title") or "").strip() or info["title"] or _fallback_title(info),
            "archived": self._is_archived(sid),
            "started": info["first_ts"],
            "updated": info["last_ts"],
            "turns": turns,
        }

    # ---------------- 续聊用 ----------------
    def to_messages(self, sid: str) -> list[dict]:
        """把历史压成 OpenAI messages（只有 user / assistant 文本）。"""
        data = self.read(sid)
        out: list[dict] = []
        for t in data["turns"]:
            text = (t.get("text") or "").strip()
            if not text:
                continue
            out.append({"role": t["role"], "content": text})
        return out

    # ---------------- 操作 ----------------
    def rename(self, sid: str, title: str) -> dict:
        title = re.sub(r"\s+", " ", (title or "")).strip()[:80]
        return self.write_meta(sid, title=title)

    def pin(self, sid: str, on: bool = True) -> dict:
        return self.write_meta(sid, pinned=bool(on))

    def archive(self, sid: str, on: bool = True):
        """归档只打标记，不挪文件。

        原因：挪文件要删源文件，而删文件在部分环境（带回收站钩子 / 文件被占用 /
        跨盘）会失败，还会留下「两边都有一份」的脏状态。打标记不可能失败，
        也方便以后加「按标签筛选」。
        """
        self.write_meta(sid, archived=bool(on))
        return {"ok": True, "archived": bool(on), "id": _safe_id(sid)}

    def delete(self, sid: str):
        sid = _safe_id(sid)
        p, mp = self._find(sid)
        p.unlink(missing_ok=True)
        mp.unlink(missing_ok=True)
        # 顺手清掉同名的报告文件
        for rp in (self.root.parent / "reports").glob(sid + "-report.md"):
            rp.unlink(missing_ok=True)
        return {"ok": True, "id": sid}

    def purge_empty(self) -> dict:
        """删掉「一次都没提过问」的会话文件。

        包括两类噪音：早期版本每次起控制台留的空文件，以及打开页面时
        自动拉数据产生的纯工具调用记录（现在内部调用已经不写日志了）。
        有过对话的会话一律不动。
        """
        removed = []
        for arch in (False, True):
            for row in self.list(archived=arch):
                if not row["turns"]:
                    try:
                        self.delete(row["id"])
                        removed.append(row["id"])
                    except Exception:
                        pass
        return {"ok": True, "removed": len(removed), "ids": removed[:50]}

    def stats(self) -> dict:
        active = self.list(archived=False)
        arch = self.list(archived=True)
        return {
            "active": len(active),
            "archived": len(arch),
            "turns": sum(r["turns"] for r in active + arch),
            "bytes": sum(r["size"] for r in active + arch),
        }
