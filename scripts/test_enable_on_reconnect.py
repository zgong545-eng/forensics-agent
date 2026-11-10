#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「重连会把未启用的服务器自动打开，并写回配置」。

背景：MCP 开关被误关后，旧代码只会回一句「重连对未启用无效，请用户去改 config.json
或重启客户端」。用户问的是问题，不是配置，这个答复等于把活推回去。
现在 sys_server_reconnect 自己打开开关并落盘。

用法：python scripts/test_enable_on_reconnect.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ForensicsAgent, Session  # noqa: E402

PASS, FAIL = 0, 0


def check(title, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[通过] {title}")
    else:
        FAIL += 1
        print(f"[失败] {title}")
    if detail:
        print(f"       {str(detail)[:200]}")


tmp = Path(tempfile.mkdtemp())
cfg_file = tmp / "config.json"
# 用一个跑不起来但不会卡住的 command：握手失败 → state=error，正好模拟「开关关着」的情形
cfg = {
    "mcpServers": {
        "demo": {
            "label": "演示服务器",
            "enabled": False,
            "command": sys.executable,
            "args": ["-c", "import sys; sys.exit(0)"],
            "env": {},
            "timeout": 8,
        }
    },
    "skills": {"roots": [], "allow_commands": []},
    "agent": {},
    "tool_timeout": 8,
}
cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

ag = ForensicsAgent(json.loads(cfg_file.read_text(encoding="utf-8")),
                    Session(enabled=False), verbose=False, cfg_path=cfg_file)

st = {s["name"]: s for s in ag.pool.status()}
check("初始状态是「未启用」", st["demo"]["state"] == "disabled", st["demo"]["state"])

text = ag.call("sys_server_reconnect", {"name": "demo"})
check("重连回执里写明「已自动开启」", "已自动开启" in text, text.replace("\n", " ")[:180])
check("回执不再叫用户改配置文件", "config.json" not in text or "写回配置" in text,
      text.replace("\n", " ")[:160])

disk = json.loads(cfg_file.read_text(encoding="utf-8"))
check("配置写回磁盘（重启后仍是开启）", disk["mcpServers"]["demo"]["enabled"] is True,
      disk["mcpServers"]["demo"].get("enabled"))
check("写回只动 enabled，其它字段保留",
      disk["mcpServers"]["demo"].get("label") == "演示服务器"
      and disk["mcpServers"]["demo"].get("args") == cfg["mcpServers"]["demo"]["args"],
      disk["mcpServers"]["demo"])
check("内存里的状态不再是 disabled",
      {s["name"]: s for s in ag.pool.status()}["demo"]["state"] != "disabled",
      {s["name"]: s for s in ag.pool.status()}["demo"]["state"])

# 再调一次：已经是启用状态，不该重复刷「已自动开启」
text2 = ag.call("sys_server_reconnect", {"name": "demo"})
check("第二次重连不重复报「已自动开启」", "已自动开启" not in text2,
      text2.replace("\n", " ")[:140])

# 状态查询：未启用时的引导语要点明「直接调 reconnect」，不能是「重连无效」
ag.pool.set_enabled("demo", False)
status_text = ag.call("sys_server_status", {})
check("状态里的未启用说明指向 reconnect（不是让用户改文件）",
      "sys_server_reconnect" in status_text and "不需要改 config.json" in status_text,
      status_text.replace("\n", " ")[:220])

ag.pool.close_all() if hasattr(ag.pool, "close_all") else None
print(f"\n=== {PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
