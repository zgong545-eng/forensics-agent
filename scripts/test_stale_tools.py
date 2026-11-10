"""验证「界面拨开关之后，Agent 的工具表会不会跟着更新」。

真凶记录（2026-10-30 现场抓到的）：
    用户在界面上把 myos-forensics 打开 → 连接池 state=ready、49 个工具
    → 但 Agent 的 self.tools 还是构造时的快照（0 个）
    → 系统提示词里同时出现「✅ myos-forensics：49 个工具」和「工具总数 0」
    → 模型照实汇报「服务器被禁用、可用工具 0」，用户连问三轮都是这句

`self.tools` 是构造时摊平的一张表，连接池是活的。只要有一处漏了重新摊平，
模型就会开始「否认事实」。所以 run() 开头必须 refresh_tools()。

用 scripts/fake_mcp.py 当靶子，不依赖取证软件、不花钱调模型。
用法：python scripts/test_stale_tools.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试隔离：经验沉淀改道到临时目录，绝不往真实的 prompts/lessons.md、
# logs/memory/qa.jsonl 里写「D 盘卷标是什么」这种假问题。
import os as _os, tempfile as _tf, shutil as _sh
_LEARN_TMP = str(ROOT / "logs" / "_test")
_os.makedirs(_LEARN_TMP, exist_ok=True)
_os.environ["LEARN_DIR"] = _LEARN_TMP

from agent import ForensicsAgent, Session  # noqa: E402

PY = sys.executable
FAKE = str(ROOT / "scripts" / "fake_mcp.py")

results = []


def check(title, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'通过' if ok else '失败'}] {title}")
    if detail and not ok:
        print(f"        → {detail}")


class ScriptedLLM:
    """按脚本回包；同时记下每次请求带的工具清单。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.tools = []
        self.seen = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.tools.append([(t.get("function") or {}).get("name") for t in (tools or [])])
        self.seen.append([dict(m) for m in messages])
        if self.script:
            return self.script.pop(0)
        return {"content": "（兜底收口）"}


def make_agent(enabled: bool):
    cfg = {
        "mcpServers": {"fake": {"command": PY, "args": [FAKE],
                                "enabled": enabled, "timeout": 20}},
        "skills": {"roots": [], "allow_commands": []},
        "agent": {}, "tool_timeout": 20,
    }
    return ForensicsAgent(cfg, Session(enabled=False), verbose=False,
                          cfg_path=ROOT / "config.json")


# ------------------------------------------------------------------ 1) 快照过期
ag = make_agent(enabled=False)
check("初始未启用 → 工具表为空", len(ag.tools) == 0, f"tools={len(ag.tools)}")

st = ag.pool.set_enabled("fake", True)          # 等价于用户在界面上拨开关
check("拨开关后连接池已就绪", bool(st and st.get("ready")),
      f"status={st}")

llm = ScriptedLLM([{"content": "当前没有任何可用工具，无法执行。"}])
ag._llm = lambda: llm
text, _ = ag.run("随便问一句", max_turns=3, tool_budget=6, renew_budget=0)

first_tools = llm.tools[0] if llm.tools else []
check("run() 开头的刷新让模型拿到了真实工具清单（不再是 0 个）",
      bool(first_tools), f"第一批工具={first_tools}")
check("提示词里的工具总数与连接池一致",
      f"工具总数 {len(ag.tools)}" in ag._system_prompt(), f"工具总数={len(ag.tools)}")

# ------------------------------------------------------------------ 2) 说谎被纠正
check("模型谎称「没有可用工具」→ 触发自校验（而不是直接把谬误当结论）",
      llm.calls >= 2 and any(
          "系统校验" in str(m.get("content") or "")
          for m in (llm.seen[1] if len(llm.seen) > 1 else [])),
      f"模型调用 {llm.calls} 次")

ag.close()

# ------------------------------------------------------------------ 3) 真的没有工具时不该瞎纠正
ag2 = make_agent(enabled=False)                 # 保持关闭，一个工具都没有
llm2 = ScriptedLLM([{"content": "当前没有任何可用工具，无法执行。"}])
ag2._llm = lambda: llm2
text2, _ = ag2.run("随便问一句", max_turns=3, tool_budget=6, renew_budget=0)
check("确实没有工具时，不触发「系统校验」误纠正（只调一次模型就收尾）",
      llm2.calls == 1 and "没有任何可用工具" in text2,
      f"模型调用 {llm2.calls} 次 / 答复={text2[:60]}")
ag2.close()

print(f"\n合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
