#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
settings_store.py — 前端可改的配置层

职责：
  * 原子读写 config.json（先写 .tmp 再 replace，断电不会写坏）
  * 模型 API Key 掩码（永不明文回传给浏览器）
  * 模型连通性测试 / 模型列表拉取（"中转站"那种体验）
  * MCP 服务器条目的校验与规范化

不引入任何第三方依赖，只用标准库。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save(path: Path, cfg: dict) -> None:
    """原子写：临时文件 + os.replace，避免写一半断电把配置写坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# API Key 掩码
# --------------------------------------------------------------------------
MASK = "*"


def mask_key(key: str) -> str:
    """sk-abcdef123456 → sk-****3456。空字符串原样返回。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return MASK * len(key)
    head = ""
    if key.lower().startswith(("sk-", "ak-", "pk-")):
        head = key[:3]
    tail = key[-4:]
    return f"{head}{MASK * 6}{tail}"


def is_masked(value: str) -> bool:
    """前端把掩码串原样提交回来时，表示"别改它"。"""
    return bool(value) and value.count(MASK) >= 3


# --------------------------------------------------------------------------
# URL 规范化
# --------------------------------------------------------------------------
_VER_RE = re.compile(r"/v\d+[a-z0-9]*$", re.I)


def normalize_base(url: str) -> str:
    """把用户输入的各种写法收敛成 base_url。

    https://api.deepseek.com/v1/chat/completions → https://api.deepseek.com/v1
    api.deepseek.com                             → https://api.deepseek.com
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    u = u.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def chat_endpoint(base: str) -> str:
    b = normalize_base(base)
    if not b:
        return ""
    if _VER_RE.search(b):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def models_endpoint(base: str) -> str:
    b = normalize_base(base)
    if not b:
        return ""
    if _VER_RE.search(b):
        return b + "/models"
    return b + "/v1/models"


# --------------------------------------------------------------------------
# 模型通信
# --------------------------------------------------------------------------
class ProbeError(Exception):
    pass


def _request(url: str, key: str, body: dict | None = None, timeout: int = 30) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "myos-forensics-agent/2.0",
            "Accept": "application/json",
        },
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise ProbeError(f"HTTP {e.code} {e.reason}" + (f" — {detail}" if detail else ""))
    except urllib.error.URLError as e:
        raise ProbeError(f"连不上：{e.reason}")
    except TimeoutError:
        raise ProbeError(f"超时（>{timeout}s）")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ProbeError("返回的不是 JSON：" + raw[:200])


def test_llm(base_url: str, api_key: str, model: str, timeout: int = 30) -> dict:
    """发一条最小请求验证 key / base / model 三者是否配套。"""
    ep = chat_endpoint(base_url)
    if not ep:
        raise ProbeError("base_url 为空")
    if not (api_key or "").strip():
        raise ProbeError("api_key 为空")
    if not (model or "").strip():
        raise ProbeError("model 为空")

    t0 = time.time()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }
    data = _request(ep, api_key.strip(), payload, timeout=timeout)
    ms = int((time.time() - t0) * 1000)

    if not isinstance(data, dict):
        raise ProbeError("响应格式异常")
    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise ProbeError(f"服务端返回错误：{msg}")

    choices = data.get("choices") or []
    reply = ""
    if choices:
        msg = choices[0].get("message") or {}
        reply = (msg.get("content") or "").strip()
        if not reply and msg.get("reasoning_content"):
            reply = "（仅返回思维链）"
    usage = data.get("usage") or {}
    return {
        "ok": True,
        "latencyMs": ms,
        "endpoint": ep,
        "serverModel": data.get("model") or model,
        "reply": reply[:120],
        "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                  if usage.get(k) is not None},
    }


def list_models(base_url: str, api_key: str, timeout: int = 20) -> list[str]:
    ep = models_endpoint(base_url)
    if not ep:
        raise ProbeError("base_url 为空")
    data = _request(ep, (api_key or "").strip(), None, timeout=timeout)
    items = data.get("data") if isinstance(data, dict) else None
    if items is None and isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise ProbeError("这个接口没返回模型列表（很多中转站不开放 /models）")
    ids = []
    for it in items:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                ids.append(str(mid))
        elif isinstance(it, str):
            ids.append(it)
    ids.sort(key=str.lower)
    return ids


# --------------------------------------------------------------------------
# 设置读写
# --------------------------------------------------------------------------
def public_settings(cfg: dict) -> dict:
    """给前端看的配置：key 只回掩码。"""
    llm = dict(cfg.get("llm") or {})
    key = str(llm.get("api_key") or "")
    env_key = os.environ.get("MYOS_LLM_API_KEY") or ""
    llm["api_key"] = mask_key(key)
    llm["hasKey"] = bool(key or env_key)
    llm["fromEnv"] = bool(env_key and not key)
    llm["base_url"] = llm.get("base_url") or ""
    llm["model"] = llm.get("model") or ""
    return {
        "llm": llm,
        "agent": cfg.get("agent") or {},
        "tool_timeout": cfg.get("tool_timeout", 300),
        "skills": cfg.get("skills") or {},
        "serverNames": list((cfg.get("mcpServers") or {}).keys()),
        "servers": cfg.get("mcpServers") or {},
    }


def apply_llm(cfg: dict, body: dict) -> dict:
    """把前端提交的模型设置合并进配置，返回是否有实质变化。"""
    llm = cfg.setdefault("llm", {})
    incoming = body.get("api_key")

    if incoming is not None:
        s = str(incoming).strip()
        if is_masked(s):
            pass                      # 掩码串 = 不动
        elif s == "":
            llm["api_key"] = ""       # 明确清空
        else:
            llm["api_key"] = s

    if "base_url" in body:
        llm["base_url"] = normalize_base(str(body.get("base_url") or ""))
    if "model" in body:
        llm["model"] = str(body.get("model") or "").strip()
    if "temperature" in body:
        try:
            llm["temperature"] = max(0.0, min(2.0, float(body["temperature"])))
        except (TypeError, ValueError):
            pass
    if "timeout" in body:
        try:
            llm["timeout"] = max(10, min(600, int(body["timeout"])))
        except (TypeError, ValueError):
            pass

    agent = cfg.setdefault("agent", {})
    if "context" in body:
        agent["context"] = str(body.get("context") or "")
    if "max_turns" in body:
        try:
            agent["max_turns"] = max(1, min(200, int(body["max_turns"])))
        except (TypeError, ValueError):
            pass
    if "tool_budget" in body:
        try:
            agent["tool_budget"] = max(10, min(1000, int(body["tool_budget"])))
        except (TypeError, ValueError):
            pass
    if "renew_budget" in body:
        try:
            agent["renew_budget"] = max(0, min(10, int(body["renew_budget"])))
        except (TypeError, ValueError):
            pass
    return cfg


# --------------------------------------------------------------------------
# MCP 服务器条目
# --------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,48}$")


def clean_server_name(name: str) -> str:
    n = (name or "").strip()
    if not _NAME_RE.match(n):
        raise ValueError("服务器名只能用字母数字和 . _ -，1~49 个字符")
    return n


def normalize_server(body: dict) -> tuple[str, dict]:
    name = clean_server_name(body.get("name") or "")
    command = str(body.get("command") or "").strip().strip('"')
    if not command:
        raise ValueError("command 不能为空（要启动的可执行文件路径）")

    args = body.get("args") or []
    if isinstance(args, str):
        args = [a for a in re.split(r"\s+", args.strip()) if a]
    if not isinstance(args, list):
        raise ValueError("args 必须是数组")

    env = body.get("env") or {}
    if isinstance(env, str):
        parsed = {}
        for line in env.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip()
        env = parsed
    if not isinstance(env, dict):
        raise ValueError("env 必须是对象")

    try:
        timeout = int(body.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(5, min(600, timeout))

    entry = {
        "label": str(body.get("label") or name).strip(),
        "kind": str(body.get("kind") or "mcp-stdio"),
        "enabled": bool(body.get("enabled", True)),
        "note": str(body.get("note") or "").strip(),
        "command": command,
        "args": [str(a) for a in args],
        "env": {str(k): str(v) for k, v in env.items()},
        "timeout": timeout,
    }
    if body.get("cwd"):
        entry["cwd"] = str(body["cwd"]).strip().strip('"')
    return name, entry


# --------------------------------------------------------------------------
# 预设（前端也能读，但放这里方便统一维护）
# --------------------------------------------------------------------------
LLM_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-chat", "deepseek-reasoner"], "note": "官方直连，便宜稳定"},
    {"id": "moonshot", "name": "Kimi / Moonshot", "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2-0905-preview"], "note": "长文本强"},
    {"id": "qwen", "name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-plus", "qwen-max", "qwen-turbo"], "note": "阿里云 DashScope 兼容模式"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"], "note": "glm-4-flash 有免费额度"},
    {"id": "siliconflow", "name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1",
     "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"], "note": "聚合中转，模型多"},
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "models": ["anthropic/claude-sonnet-4", "google/gemini-2.5-pro"], "note": "海外聚合，需网络"},
    {"id": "ollama", "name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1",
     "models": ["qwen2.5:14b", "deepseek-r1:14b"], "note": "本机跑，key 随便填"},
    {"id": "custom", "name": "自定义 / 中转站", "base_url": "", "models": [], "note": "填你自己的地址"},
]
