#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
webui.py — 电子数据取证智能体 · 可视化控制台

零第三方依赖（标准库 http.server + SSE），复用 agent.py 的 MCP 客户端与 LLM 适配层。

启动：
    python webui.py                # 默认 http://127.0.0.1:8765 自动开浏览器
    python webui.py --port 9000
    python webui.py --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent import (BASE_DIR, DEFAULT_CONFIG, DESTRUCTIVE_TOOLS, LlmError,
                   McpError, McpServerHandle, ForensicsAgent, RunControl, Session,
                   load_config)
import settings_store as store
from sessions import SessionStore

if sys.platform == "win32":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

UI_DIR = BASE_DIR / "ui"
CHAT_LOCK = threading.Lock()

STATE = {
    "agent": None,
    "session": None,
    "history": None,
    "auto_yes": False,
    "error": None,
    "myos_ready": False,
    "config_path": str(DEFAULT_CONFIG),
    "started_at": time.time(),
    # 启动时后端代码的指纹。磁盘上更新了就说明「改了代码但没重启」，
    # 浏览器刷新不会重新加载 Python，这个提示能省掉大量「改了怎么没用」的困惑。
    "code_sig": "",
    # 模型连通性探测缓存：{signature: {...}}
    "llm_probe": None,
    # 历史会话（logs/*.jsonl + logs/archive/*.jsonl）
    "sessions": SessionStore(BASE_DIR / "logs"),
}


# --------------------------------------------------------------------------
# 配置读写（前端可改）
# --------------------------------------------------------------------------
_BACKEND_FILES = ("agent.py", "webui.py", "skills.py", "settings_store.py", "sessions.py")


def code_sig() -> str:
    """后端源码的最新修改时间。用来判断磁盘上的代码比运行中的新。"""
    newest = 0.0
    for name in _BACKEND_FILES:
        try:
            newest = max(newest, (BASE_DIR / name).stat().st_mtime)
        except OSError:
            pass
    return f"{newest:.0f}"


def cfg_path() -> Path:
    return Path(STATE["config_path"])


def read_cfg() -> dict:
    """每次都从磁盘读——保证前端改完立刻生效，也避免内存里两份配置打架。"""
    return store.load(cfg_path())


def write_cfg(cfg: dict) -> None:
    store.save(cfg_path(), cfg)
    # 就地同步给在跑的 agent，避免为了改个 key 就重连全部 MCP
    agent = STATE.get("agent")
    if agent:
        agent.cfg = cfg


def _apply_model_to_agent(cfg: dict) -> None:
    agent = STATE.get("agent")
    if agent:
        agent.cfg["llm"] = cfg.get("llm") or {}
        agent.cfg["agent"] = cfg.get("agent") or {}



# --------------------------------------------------------------------------
# Agent 生命周期
# --------------------------------------------------------------------------
def probe_ready(agent) -> bool:
    """探一次活：任一服务器返回 ready=true 即视为取证运行时就绪。"""
    try:
        res = agent.health_check()
        return bool(res.get("ok"))
    except Exception:
        return False


def boot_agent() -> dict:
    """启动（或重启）全部 MCP + agent。返回状态摘要。"""
    old = STATE.get("agent")
    if old:
        try:
            old.close()
        except Exception:
            pass
        STATE["agent"] = None
        STATE["myos_ready"] = False

    if not STATE.get("session"):
        STATE["session"] = Session(enabled=True)

    try:
        cfg = read_cfg()
        agent = ForensicsAgent(
            cfg, STATE["session"],
            auto_yes=STATE["auto_yes"],
            allow_destructive=STATE["auto_yes"],
            verbose=False,
            cfg_path=cfg_path(),
        )
        agent.auto_yes = STATE["auto_yes"]
        agent.allow_destructive = STATE["auto_yes"]
        STATE["agent"] = agent
        STATE["history"] = None
        STATE["error"] = None
        # 启动就探一次活：D-Bus 不通时 MCP 只会在首次调工具时才退出，别等用户踩坑
        STATE["myos_ready"] = probe_ready(agent)
        return status()
    except Exception as e:
        STATE["error"] = f"{type(e).__name__}: {e}"
        STATE["agent"] = None
        STATE["myos_ready"] = False
        return status()


def status() -> dict:
    agent = STATE.get("agent")
    try:
        cfg = read_cfg()
    except Exception:
        cfg = {}
    llm = cfg.get("llm") or {}
    key = str(llm.get("api_key") or "")
    env_key = os.environ.get("MYOS_LLM_API_KEY") or ""
    has_key = bool(env_key or key)
    # 环境变量优先级高于 config.json（agent._llm 就是这么取的），状态栏要显示真实生效的那个
    eff_model = os.environ.get("MYOS_LLM_MODEL") or llm.get("model") or ""
    eff_base = os.environ.get("MYOS_LLM_BASE_URL") or llm.get("base_url") or ""

    base = {
        "model": eff_model,
        "baseUrl": eff_base,
        "keyMask": store.mask_key(key) or ("来自环境变量" if env_key else ""),
        "hasKey": has_key,
        "envOverride": bool(os.environ.get("MYOS_LLM_MODEL") or os.environ.get("MYOS_LLM_BASE_URL")
                            or env_key),
        "autoYes": STATE["auto_yes"],
        "error": STATE.get("error"),
        "sessionId": (STATE["session"].id if STATE.get("session") else ""),
        "resumed": bool(STATE.get("history")),
        # 有 control 就说明对话线程正在跑，前端据此决定发消息是「提问」还是「插话」
        "running": bool(STATE.get("control")),
        "queuedInject": (STATE["control"].pending() if STATE.get("control") else 0),
        # 能不能对话，取决于「agent 建成没有」，而不是「MCP 连上没有」。
        # agent 建成后即使一台取证服务器都没接上，技能（sys_skill_*）和纯问答照样能跑，
        # 前端不该因此把输入框锁死。
        "agentReady": bool(agent),
        # 磁盘上的后端代码比进程里的新 → 改了没重启，很多「修复不生效」都是这个
        "needRestart": bool(STATE.get("code_sig")) and STATE["code_sig"] != code_sig(),
    }

    if not agent:
        base.update({"connected": False, "agentReady": False, "myosReady": False, "toolCount": 0,
                     "servers": [], "skillCount": 0, "skillGroups": []})
        return base

    servers = agent.pool.status()
    ready = [s for s in servers if s["ready"]]
    failed = [s for s in servers if s["state"] == "error"]
    if failed and not ready and not STATE.get("error"):
        STATE["error"] = "全部 MCP 服务器连接失败：" + "；".join(
            f"{s['name']}（{s['error']}）" for s in failed[:2])

    base.update({
        "connected": bool(ready),
        "myosReady": bool(STATE.get("myos_ready")),
        "servers": servers,
        "server": ready[0]["server"] if ready else None,
        "version": ready[0]["version"] if ready else None,
        "protocol": ready[0]["protocol"] if ready else None,
        "toolCount": len(agent.tools),
        "readyCount": len(ready),
        "serverCount": len(servers),
        "skillCount": len(agent.skills.skills),
        "skillGroups": [{"title": g["title"], "count": g["count"]}
                        for g in agent.skills.groups()],
        "historyLen": len(STATE.get("history") or []),
    })
    return base


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _note_failure(text: str) -> None:
    """本轮异常也要落一条记录。

    不然历史会话里只有「提问」没有「回答」，看起来像答案丢了——
    实际是压根没写进去。出错也要留痕，用户翻历史时才知道当时发生了什么。
    """
    s = STATE.get("session")
    if not s:
        return
    try:
        s.log("assistant_final", text=f"（本轮没有正常答复）{text}", error=True)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "MYOSAgentUI/1.0"

    def log_message(self, fmt, *args):
        pass  # 静音访问日志

    # ---------------- 工具方法 ----------------
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", errors="replace") or "{}")
        except Exception:
            return {}

    def _static(self, rel: str):
        rel = rel.lstrip("/") or "index.html"
        target = (UI_DIR / rel).resolve()
        try:
            target.relative_to(UI_DIR.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._json({"error": "not found", "path": rel}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------- GET ----------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/api/"):
            if path == "/api/status":
                return self._json(status())
            if path == "/api/tools":
                agent = STATE.get("agent")
                if not agent:
                    return self._json({"error": STATE.get("error") or "未连接"}, 503)
                tools = []
                for t in agent.tool_index():
                    schema = t["inputSchema"] or {}
                    tools.append({
                        "name": t["name"],
                        "key": t["key"],
                        "server": t["server"],
                        "description": t["description"].strip(),
                        "required": schema.get("required") or [],
                        "properties": list((schema.get("properties") or {}).keys()),
                        "schema": schema,
                        "dangerous": t["dangerous"],
                    })
                return self._json({"tools": tools, "servers": agent.pool.status()})
            if path == "/api/servers":
                agent = STATE.get("agent")
                if not agent:
                    return self._json({"servers": [], "error": STATE.get("error")})
                return self._json({"servers": agent.pool.status()})
            if path == "/api/skills":
                agent = STATE.get("agent")
                if not agent:
                    return self._json({"groups": [], "error": STATE.get("error")})
                return self._json({
                    "groups": agent.skills.groups(),
                    "summary": agent.skills.summary(),
                })
            if path == "/api/skill":
                agent = STATE.get("agent")
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                if not agent:
                    return self._json({"error": STATE.get("error") or "未连接"}, 503)
                sk = agent.skills.get(name)
                if not sk:
                    return self._json({"error": f"未找到技能 {name}"}, 404)
                d = sk.to_dict(with_body=True)
                d["allowCommands"] = agent.skills.allow_commands
                return self._json(d)
            if path == "/api/chat/state":
                c = STATE.get("control")
                return self._json({
                    "running": bool(c),
                    "interrupted": bool(c and c.stopped()),
                    "queued": (c.pending() if c else 0),
                    "consumed": (c.consumed if c else 0),
                })
            if path == "/api/sessions":
                q = parse_qs(urlparse(self.path).query)
                want_arch = (q.get("archived") or ["0"])[0] in ("1", "true", "yes")
                st = STATE["sessions"]
                try:
                    rows = st.list(archived=want_arch)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                return self._json({
                    "sessions": rows,
                    "archived": want_arch,
                    "stats": st.stats(),
                    "currentId": (STATE["session"].id if STATE.get("session") else ""),
                })
            if path == "/api/session":
                q = parse_qs(urlparse(self.path).query)
                sid = (q.get("id") or [""])[0]
                try:
                    return self._json(STATE["sessions"].read(sid))
                except FileNotFoundError:
                    return self._json({"error": "会话不存在"}, 404)
                except Exception as e:
                    return self._json({"error": str(e)}, 400)
            if path == "/api/cases":
                agent = STATE.get("agent")
                if not agent:
                    return self._json({"ok": False, "cases": [],
                                       "error": STATE.get("error") or "未连接"})
                try:
                    with CHAT_LOCK:
                        text = agent.call("myos-forensics.query_cases", {},
                                          quiet=True, log_it=False)
                    return self._json({"ok": True, "result": text})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
            if path == "/api/settings":
                try:
                    cfg = read_cfg()
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                data = store.public_settings(cfg)
                data["configPath"] = str(cfg_path())
                data["presets"] = store.LLM_PRESETS
                data["autoYes"] = STATE["auto_yes"]
                # roots 里可能有相对路径（./skills），光看配置值不知道它落在哪，
                # 把解析后的绝对路径 + 各自贡献的技能数一并给前端
                ag = STATE.get("agent")
                if ag:
                    data["skills"]["resolved"] = ag.skills.root_detail()
                    data["skills"]["count"] = len(ag.skills.skills)
                return self._json(data)
            if path == "/api/config":
                try:
                    cfg = read_cfg()
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                return self._json(store.public_settings(cfg))
            return self._json({"error": "unknown api"}, 404)
        return self._static(path)

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/settings":
            return self._save_settings(body)
        if path == "/api/llm/test":
            return self._llm_test(body)
        if path == "/api/llm/models":
            return self._llm_models(body)
        if path == "/api/server/upsert":
            return self._server_upsert(body)
        if path == "/api/server/delete":
            return self._server_delete(body)
        if path == "/api/server/test":
            return self._server_test(body)
        if path == "/api/chat":
            return self._chat(body)
        if path == "/api/chat/stop":
            # 中断：只置位，真正收手由对话线程在下一个检查点做。
            # 这里立刻返回，前端不用等——不然按钮要转好几秒才有反应。
            c = STATE.get("control")
            if not c:
                return self._json({"ok": False, "error": "当前没有正在跑的推理"}, 409)
            c.stop("user")
            return self._json({"ok": True, "queued": c.pending()})
        if path == "/api/chat/inject":
            # 插话：进队列，下一轮开头注入成 user 消息
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "插话内容为空"}, 400)
            c = STATE.get("control")
            if not c:
                return self._json({"ok": False, "error": "当前没有正在跑的推理，"
                                                      "直接把这句话当新问题发就行"}, 409)
            c.push(text)
            return self._json({"ok": True, "queued": c.pending()})
        if path == "/api/session/rename":
            try:
                meta = STATE["sessions"].rename(body.get("id"), body.get("title") or "")
                return self._json({"ok": True, "meta": meta})
            except FileNotFoundError:
                return self._json({"error": "会话不存在"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/session/pin":
            try:
                meta = STATE["sessions"].pin(body.get("id"), bool(body.get("on")))
                return self._json({"ok": True, "meta": meta})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/session/archive":
            try:
                return self._json(STATE["sessions"].archive(body.get("id"), bool(body.get("on", True))))
            except FileNotFoundError:
                return self._json({"error": "会话不存在"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/session/delete":
            # 支持单删（id）和批量（ids）。批量删除不能一条失败就整体报错，
            # 否则删到一半停住，用户不知道到底删了几个。
            ids = body.get("ids")
            if isinstance(ids, list):
                done, failed = [], []
                for one in ids:
                    try:
                        STATE["sessions"].delete(str(one))
                        done.append(one)
                    except Exception as e:
                        failed.append({"id": one, "error": str(e)})
                # 删掉的正好是当前会话，就换个新的，免得后面续写往坟里写
                cur = STATE["session"].id if STATE.get("session") else ""
                if cur in [str(x) for x in done]:
                    try:
                        STATE["session"].close()
                    except Exception:
                        pass
                    STATE["session"] = Session(enabled=True)
                    STATE["history"] = None
                return self._json({"ok": not failed, "deleted": len(done),
                                   "failed": failed, "deletedIds": done})
            try:
                return self._json(STATE["sessions"].delete(body.get("id")))
            except FileNotFoundError:
                return self._json({"error": "会话不存在"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/session/purge":
            try:
                return self._json(STATE["sessions"].purge_empty())
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if path == "/api/session/open":
            return self._session_open(body)
        if path == "/api/call":
            return self._call(body)
        if path == "/api/health":
            agent = STATE.get("agent")
            if not agent:
                return self._json({"connected": False, "error": STATE.get("error")}, 503)
            with CHAT_LOCK:
                res = agent.health_check()
            STATE["myos_ready"] = bool(res.get("ok"))
            res["connected"] = any(s["ready"] for s in res.get("servers") or [])
            return self._json(res)
        if path == "/api/server/toggle":
            name = body.get("name") or ""
            on = bool(body.get("on"))
            # 先落盘：即使 agent 没起来，配置也该记住这次开关
            try:
                cfg = read_cfg()
                cfg.setdefault("mcpServers", {}).setdefault(name, {})["enabled"] = on
                write_cfg(cfg)
            except Exception:
                pass
            agent = STATE.get("agent")
            if not agent:
                return self._json({"ok": True, "saved": True, "server": None,
                                   "note": "配置已保存，下次启动生效"})
            with CHAT_LOCK:
                st = agent.pool.set_enabled(name, on)
                agent.tools, agent._routes = agent._collect_tools()
            if st is None:
                return self._json({"ok": False, "error": f"未知服务器 {name}"}, 404)
            return self._json({"ok": True, "server": st, "toolCount": len(agent.tools)})
        if path == "/api/server/reconnect":
            agent = STATE.get("agent")
            if not agent:
                return self._json({"ok": False, "error": STATE.get("error")}, 503)
            name = (body.get("name") or "").strip() or None
            with CHAT_LOCK:
                agent.pool.reconnect(name)
                agent.tools, agent._routes = agent._collect_tools()
                servers = agent.pool.status()
            STATE["myos_ready"] = any(s["ready"] for s in servers)
            if any(s["ready"] for s in servers):
                STATE["error"] = None
            return self._json({"ok": True, "servers": servers, "toolCount": len(agent.tools)})
        if path == "/api/skill/exec":
            agent = STATE.get("agent")
            if not agent:
                return self._json({"ok": False, "error": STATE.get("error")}, 503)
            sk_name = (body.get("name") or "").strip()
            cmd = (body.get("command") or "").strip()
            timeout = int(body.get("timeout") or 300)
            sk = agent.skills.get(sk_name)
            if not sk:
                return self._json({"ok": False, "error": f"未找到技能 {sk_name}"}, 404)
            if agent.skills.is_write_command(cmd) and not STATE["auto_yes"]:
                return self._json({
                    "ok": False, "blocked": True,
                    "error": "这条命令看起来会改动数据，未获授权未执行。开启右上「允许写操作」后可执行。",
                }, 403)
            with CHAT_LOCK:
                t0 = time.time()
                rc, text = agent.skills.exec(sk_name, cmd, timeout=timeout)
            return self._json({"ok": rc == 0, "exit": rc, "result": text,
                               "elapsed": round(time.time() - t0, 2)})
        if path == "/api/reset":
            STATE["history"] = None
            old = STATE.get("session")
            if old:
                old.close()
            STATE["session"] = Session(enabled=True)
            return self._json({"ok": True, "sessionId": STATE["session"].id})
        if path == "/api/autoyes":
            on = bool(body.get("on"))
            STATE["auto_yes"] = on
            agent = STATE.get("agent")
            if agent:
                agent.auto_yes = on
                agent.allow_destructive = on
            return self._json({"ok": True, "autoYes": on})
        if path == "/api/reconnect":
            with CHAT_LOCK:
                return self._json(boot_agent())
        return self._json({"error": "unknown api"}, 404)

    # ---------------- 设置：模型 ----------------
    def _save_settings(self, body):
        try:
            cfg = read_cfg()
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

        before = json.dumps(cfg.get("llm") or {}, sort_keys=True)
        store.apply_llm(cfg, body)

        if "tool_timeout" in body:
            try:
                cfg["tool_timeout"] = max(10, min(1800, int(body["tool_timeout"])))
            except (TypeError, ValueError):
                pass

        sk = body.get("skills")
        if isinstance(sk, dict):
            cur = cfg.setdefault("skills", {})
            if isinstance(sk.get("roots"), list):
                cur["roots"] = [str(x) for x in sk["roots"] if str(x).strip()]
            if isinstance(sk.get("allow_commands"), list):
                cur["allow_commands"] = [str(x).strip() for x in sk["allow_commands"] if str(x).strip()]

        try:
            write_cfg(cfg)
        except OSError as e:
            return self._json({"ok": False, "error": f"写入失败：{e}"}, 500)

        _apply_model_to_agent(cfg)
        after = json.dumps(cfg.get("llm") or {}, sort_keys=True)
        STATE["llm_probe"] = None
        return self._json({
            "ok": True,
            "changed": before != after,
            "settings": store.public_settings(cfg),
        })

    def _key_for(self, body, saved_llm) -> str:
        """前端传掩码或空 → 回退到磁盘 / 环境变量里的真 key。"""
        k = str(body.get("api_key") or "").strip()
        if k and not store.is_masked(k):
            return k
        return os.environ.get("MYOS_LLM_API_KEY") or str(saved_llm.get("api_key") or "")

    def _llm_test(self, body):
        cfg = read_cfg()
        saved = cfg.get("llm") or {}
        key = self._key_for(body, saved)
        base = body.get("base_url") or saved.get("base_url") or ""
        model = body.get("model") or saved.get("model") or ""
        try:
            res = store.test_llm(base, key, model, timeout=int(body.get("timeout") or 30))
        except store.ProbeError as e:
            return self._json({"ok": False, "error": str(e), "endpoint": store.chat_endpoint(base)})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        STATE["llm_probe"] = {"at": time.time(), "model": model, "ok": True}
        return self._json(res)

    def _llm_models(self, body):
        cfg = read_cfg()
        saved = cfg.get("llm") or {}
        key = self._key_for(body, saved)
        base = body.get("base_url") or saved.get("base_url") or ""
        try:
            ids = store.list_models(base, key, timeout=int(body.get("timeout") or 20))
        except store.ProbeError as e:
            return self._json({"ok": False, "error": str(e), "endpoint": store.models_endpoint(base)})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self._json({"ok": True, "models": ids, "endpoint": store.models_endpoint(base)})

    # ---------------- 设置：MCP 服务器 ----------------
    def _server_upsert(self, body):
        try:
            name, entry = store.normalize_server(body)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

        cfg = read_cfg()
        servers = cfg.setdefault("mcpServers", {})
        original = str(body.get("originalName") or "").strip()

        agent = STATE.get("agent")
        if original and original != name:
            servers.pop(original, None)
            if agent:
                agent.pool.remove(original)
        servers[name] = entry

        try:
            write_cfg(cfg)
        except OSError as e:
            return self._json({"ok": False, "error": f"写入失败：{e}"}, 500)

        if not agent:
            return self._json({"ok": True, "saved": True, "server": None,
                               "note": "配置已保存，下次启动生效"})
        with CHAT_LOCK:
            st = agent.pool.upsert(name, entry)
            agent.tools, agent._routes = agent._collect_tools()
        STATE["myos_ready"] = any(s["ready"] for s in agent.pool.status())
        return self._json({"ok": True, "server": st, "toolCount": len(agent.tools),
                           "servers": agent.pool.status()})

    def _server_delete(self, body):
        name = str(body.get("name") or "").strip()
        if not name:
            return self._json({"ok": False, "error": "缺少 name"}, 400)
        cfg = read_cfg()
        servers = cfg.get("mcpServers") or {}
        if name not in servers:
            return self._json({"ok": False, "error": f"没有这台服务器：{name}"}, 404)
        servers.pop(name)
        cfg["mcpServers"] = servers
        try:
            write_cfg(cfg)
        except OSError as e:
            return self._json({"ok": False, "error": f"写入失败：{e}"}, 500)
        agent = STATE.get("agent")
        if agent:
            with CHAT_LOCK:
                agent.pool.remove(name)
                agent.tools, agent._routes = agent._collect_tools()
        return self._json({"ok": True, "toolCount": len(agent.tools) if agent else 0,
                           "servers": agent.pool.status() if agent else []})

    def _server_test(self, body):
        try:
            name, entry = store.normalize_server(body)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        entry["enabled"] = True
        entry["timeout"] = min(int(entry.get("timeout") or 60), 60)
        h = McpServerHandle(name, entry, entry["timeout"])
        t0 = time.time()
        try:
            h.connect()
            st = h.status()
        finally:
            h.close()
        st["elapsedMs"] = int((time.time() - t0) * 1000)
        return self._json({"ok": bool(st.get("ready")), "server": st})

    # ---------------- 业务 ----------------
    def _call(self, body):
        agent = STATE.get("agent")
        if not agent:
            return self._json({"error": STATE.get("error") or "未连接"}, 503)
        name = body.get("name") or ""
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return self._json({"error": "args 必须是对象"}, 400)
        with CHAT_LOCK:
            try:
                t0 = time.time()
                text = agent.call(name, args, quiet=True)
                return self._json({"ok": True, "elapsed": round(time.time() - t0, 2), "result": text,
                                   "blocked": "【已拦截】" in text})
            except McpError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _session_open(self, body):
        """把某个历史会话载入成「当前会话」：恢复模型上下文 + 后续对话续写同一个文件。"""
        sid = body.get("id") or ""
        store = STATE["sessions"]
        try:
            data = store.read(sid)
            msgs = store.to_messages(sid)
            path = store.path_of(sid)
        except FileNotFoundError:
            return self._json({"error": "会话不存在"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 400)

        agent = STATE.get("agent")
        system = agent.new_history() if agent else [{"role": "system", "content": ""}]
        STATE["history"] = system + msgs

        old = STATE.get("session")
        if old:
            old.close()
        STATE["session"] = Session(enabled=True, path=path)
        return self._json({
            "ok": True,
            "id": sid,
            "title": data["title"],
            "archived": data["archived"],
            "resumed": len(msgs),
            "turns": data["turns"],
        })

    def _chat(self, body):
        agent = STATE.get("agent")
        prompt = (body.get("text") or "").strip()
        if not prompt:
            return self._json({"error": "空问题"}, 400)
        if not agent:
            return self._json({"error": STATE.get("error")
                               or "后端 agent 没起来（多数是配置读不出来或 Python 报错），"
                                  "看控制台窗口的输出"}, 503)

        # SSE 头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        closed = {"v": False}

        def emit(ev: dict):
            if closed["v"]:
                return
            try:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                closed["v"] = True

        got_lock = CHAT_LOCK.acquire(timeout=1)
        if not got_lock:
            emit({"type": "error", "message": "上一条还在跑，等它结束。"})
            emit({"type": "done"})
            return
        ctrl = RunControl()
        STATE["control"] = ctrl
        try:
            if STATE.get("history") is None:
                STATE["history"] = agent.new_history()
            emit({"type": "start", "text": prompt})
            # 预算优先级：本次请求 > config.json → agent.* > 默认
            ag = {}
            try:
                ag = read_cfg().get("agent") or {}
            except Exception:
                ag = {}

            def _num(key, default, lo, hi):
                try:
                    return max(lo, min(hi, int(ag.get(key) or default)))
                except (TypeError, ValueError):
                    return default

            cfg_turns = _num("max_turns", 40, 1, 200)
            cfg_calls = _num("tool_budget", 120, 10, 1000)
            cfg_renew = _num("renew_budget", 3, 0, 10)
            answer, history = agent.run(
                prompt,
                max_turns=int(body.get("maxTurns") or cfg_turns),
                tool_budget=int(body.get("toolBudget") or cfg_calls),
                renew_budget=cfg_renew,
                history=STATE["history"],
                on_event=emit,
                control=ctrl,
            )
            STATE["history"] = history
            if body.get("save"):
                p = STATE["session"].save_report(answer)
                emit({"type": "saved", "path": str(p)})
        except LlmError as e:
            emit({"type": "error", "message": "模型错误：" + str(e)})
            _note_failure("模型错误：" + str(e))
        except McpError as e:
            emit({"type": "error", "message": "MCP 错误：" + str(e)})
            _note_failure("MCP 错误：" + str(e))
        except Exception as e:
            emit({"type": "error", "message": f"{type(e).__name__}: {e}",
                  "detail": traceback.format_exc()[-1500:]})
            _note_failure(f"{type(e).__name__}: {e}")
        finally:
            CHAT_LOCK.release()
            if STATE.get("control") is ctrl:
                STATE["control"] = None
            emit({"type": "done"})
            closed["v"] = True


# --------------------------------------------------------------------------

class ConsoleServer(ThreadingHTTPServer):
    """控制台的 HTTP 服务。

    Windows 上 `SO_REUSEADDR` 的语义和 Linux 不同：它允许**两个进程绑同一个端口**，
    后绑的那个"成功"了却收不到任何连接。用户实测踩过——他重开一个 `python webui.py`，
    进程活着、控制台却一片空白（其实流量全被前一个进程接走了）。
    所以 Windows 上要关掉它，让端口冲突老老实实报错。
    """

    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="取证智能体可视化控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args()

    STATE["config_path"] = args.config
    STATE["session"] = Session(enabled=True)
    STATE["code_sig"] = code_sig()

    url = f"http://{args.host}:{args.port}/"
    # 先占端口，再连 MCP。顺序很重要：端口被占时立刻报错退出，
    # 不要先去连一遍取证软件、等 20 秒握手超时才崩。
    # （用户实测过：屏幕外开着上一份控制台，于是他在旧代码上继续测，改动全都不生效。）
    try:
        srv = ConsoleServer((args.host, args.port), Handler)
    except OSError:
        print(f"\n[×] 端口 {args.port} 已经被占用——多半是另一个控制台还在跑。")
        print(f"    ① 关掉那个窗口（或结束占用进程），然后重跑本命令；")
        print(f"    ② 或者换个端口：python webui.py --port {args.port + 1}")
        print(f"    注意：占着端口的是**旧代码**，你在浏览器里看到的改动都不会生效。\n")
        raise SystemExit(1)
    srv.daemon_threads = True

    print("正在连接「超级取证大师」…")
    st = boot_agent()
    if st.get("connected"):
        print(f"[OK] {st['server']} v{st['version']}  protocol={st['protocol']}  工具 {st['toolCount']} 个")
    else:
        print(f"[!] 未连上：{st.get('error')}")
        print("    先启动 ForensicDesktop.exe，再打开控制台点「重连」即可。")
    print(f"\n控制台： {url}\n（Ctrl+C 退出）\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        srv.shutdown()
        agent = STATE.get("agent")
        if agent:
            try:
                agent.close()
            except Exception:
                pass
        if STATE.get("session"):
            STATE["session"].close()


if __name__ == "__main__":
    main()
