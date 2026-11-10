"""验证「无证据的硬断言」会被拦住，以及熔断能半开复活。

现场事故（2026-10-30 14:05，用户亲测）：
  同一道「开机密码是多少（答案格式：如 123456）」被问三遍：
    第 1 遍 → 「无法得出」
    第 2 遍 → 「查不到」
    第 3 遍 → 「密码为 123456」，还配了一张
              「SYSTEM 取 bootkey → 解密 SAM 的 V 值 → 得 NTLM → 字典命中」的证据表
  而当时 MCP 全被用户关掉，一条工具都没跑过。这不是知识错误，是**迎合 + 编证据链**。

三道闸（本文件逐条锁）：
  1. 题目里的「如 XXX」示例值不再进模型（scrub_examples）
  2. 一次工具都没成功返回时，禁止给具体值（证据门禁）
  3. 上一轮说「未取得」，追问后就改口 → 打回（迎合检测）

用法：python scripts/test_evidence_gate.py
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

from agent import ForensicsAgent, Session, scrub_examples  # noqa: E402

PY = sys.executable
FAKE = str(ROOT / "scripts" / "fake_mcp.py")
results = []


def check(title, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'通过' if ok else '失败'}] {title}")
    if detail and not ok:
        print(f"        → {detail}")


class ScriptedLLM:
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
        return {"content": "（兜底）"}


def make_agent(enabled=True):
    cfg = {
        "mcpServers": {"fake": {"command": PY, "args": [FAKE],
                                "enabled": enabled, "timeout": 20}},
        "skills": {"roots": [], "allow_commands": []},
        "agent": {}, "tool_timeout": 20,
    }
    return ForensicsAgent(cfg, Session(enabled=False), verbose=False,
                          cfg_path=ROOT / "config.json")


def call_ping(i=0):
    return {"content": "", "tool_calls": [{
        "id": f"c{i}", "type": "function",
        "function": {"name": "ping", "arguments": "{}"}}]}


# ---------------------------------------------------------------- 1) 示例值脱敏
q = "该计算机的开机密码是多少？（答案格式：以实际为准，如123456）"
s = scrub_examples(q)
check("题目里的示例值被抹掉，格式说明还在",
      "123456" not in s and "答案格式" in s and "示例" in s, s)

q2 = "该计算机曾接入一个移动硬盘，分配驱动器号为D盘，请问盘符名称为？（答案格式：以实际为准，如Tuo_Jie）"
s2 = scrub_examples(q2)
check("「如Tuo_Jie」同样被抹掉", "Tuo_Jie" not in s2, s2)

plain = "如果镜像打不开，就先 load_case 再 query_device_info。"
check("普通句子里的「如果」不受影响", scrub_examples(plain) == plain, scrub_examples(plain))

# ---------------------------------------------------------------- 2) 无工具 + 硬断言
ag = make_agent(enabled=True)
llm = ScriptedLLM([
    {"content": "**结论：该计算机的开机密码为 `123456`。**\n\n"
                "| 项目 | 内容 |\n|---|---|\n| 提取方式 | SYSTEM 取 bootkey → 解密 SAM 的 V 值 → NTLM → 字典命中 |"},
    {"content": "**结论：未取得。** 本轮没有可用的取证工具，无法读取 SAM 完成哈希还原。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机的开机密码是多少？（答案格式：以实际为准，如123456）",
                 max_turns=6, tool_budget=10, renew_budget=0, on_event=events.append)

check("编造的证据链被拦下（发出 gate 事件）",
      any(e.get("type") == "gate" for e in events),
      [e.get("type") for e in events])
check("被打回后模型重写为「未取得」，最终答复不是 123456",
      "123456" not in text and "未取得" in text, text[:120])
gate_msg = ""
for m in (llm.seen[1] if len(llm.seen) > 1 else []):
    if "证据门禁" in str(m.get("content") or ""):
        gate_msg = str(m.get("content"))
check("打回时明确告诉它「没有工具结果就不能给具体值」",
      "没有任何一次" in gate_msg and "未取得" in gate_msg, gate_msg[:150])
check("送进模型的题目里已经没有 123456（系统提示词也不算数）",
      all("123456" not in str(m.get("content") or "")
          for m in (llm.seen[0] if llm.seen else []) if m.get("role") != "system"),
      [str(m.get("content"))[:80] for m in (llm.seen[0] if llm.seen else [])
       if "123456" in str(m.get("content") or "")])
ag.close()

# ---------------------------------------------------------------- 3) 有工具结果就放行
ag = make_agent(enabled=True)
llm = ScriptedLLM([
    call_ping(0),
    {"content": "**结论：卷标为 `Samsung_T5`。** 依据：query 返回 name = Samsung_T5。"},
])
# 让「查询」真的返回这个卷标：引用核对要求值出现在原文里，编的值不该放行
ag.call = lambda name, args, quiet=False, on_event=None, log_it=True: \
    "columns: name | rows: Samsung_T5 | rowCount: 1"
ag._llm = lambda: llm
events = []
text, _ = ag.run("D 盘卷标是什么", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
check("有工具成功返回时，正常结论不被误拦",
      not any(e.get("type") == "gate" for e in events) and "Samsung_T5" in text,
      f"gate={[e for e in events if e.get('type') == 'gate']} / {text[:80]}")
ag.close()

# ---------------------------------------------------------------- 4) 迎合：追问后改口
hist = [
    {"role": "user", "content": "该计算机的开机密码是多少？（答案格式：以实际为准，如123456）"},
    {"role": "assistant", "content": "**结论：查不到。** 镜像里没有可直接读取的开机密码。"},
]
ag = make_agent(enabled=False)          # 一个工具都没有，模拟用户把 MCP 全关
llm = ScriptedLLM([
    {"content": "**结论：该计算机的开机密码为 `123456`。**"},
    {"content": "**结论：仍然查不到。** 解释：磁盘上只有 NTLM 哈希，需要额外工具才能还原。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机的开机密码是多少？（答案格式：以实际为准，如123456）",
                 max_turns=6, tool_budget=10, renew_budget=0,
                 history=hist, on_event=events.append)
check("上一轮说「查不到」，本轮无新证据却改口 → 被打回",
      any(e.get("type") == "gate" for e in events),
      [e.get("type") for e in events])
check("最终没有把 123456 交出去", "123456" not in text, text[:120])
ag.close()

# ---------------------------------------------------------------- 5) 只读了技能/状态不算证据
ag = make_agent(enabled=False)          # 关掉 MCP：只剩内置工具
llm = ScriptedLLM([
    {"content": "", "tool_calls": [{
        "id": "s1", "type": "function",
        "function": {"name": "sys_server_status", "arguments": "{}"}}]},
    {"content": "**结论：本地账户 `pc` 的登录密码为 `260919`。**\n\n"
                "依据：`ges read /case/2/基本信息/用户列表/data.csv` 返回含「登录密码」列。"},
    {"content": "**结论：未取得。** 只查到服务器状态，没有拿到任何案件数据。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机的开机密码是多少？（答案格式：以实际为准，如123456）",
                 max_turns=6, tool_budget=10, renew_budget=0, on_event=events.append)
check("只调过 sys_server_status / sys_skill_read 时，仍不许给具体值",
      any(e.get("type") == "gate" for e in events) and "260919" not in text,
      f"events={[e.get('type') for e in events]} text={text[:100]}")
ag.close()

# ---------------------------------------------------------------- 6) 引用核对：值必须在原文里
ag = make_agent(enabled=True)           # ping 只会回 "pong"，不含任何数字
llm = ScriptedLLM([
    call_ping(0),
    {"content": "**结论：本地账户 `pc` 的登录密码为 `260919`。**\n"
                "依据：`ping` 返回里就带着这个值。"},
    {"content": "**结论：未取得。** 工具只返回了 pong，没有账户数据。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机的开机密码是多少？", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
check("引用的值在工具原文里找不到 → 打回（口头证据不算证据）",
      any(e.get("type") == "gate" for e in events) and "260919" not in text,
      f"events={[e.get('type') for e in events]} text={text[:100]}")

# 反过来：值真的在返回里 → 不许误拦
ag2 = make_agent(enabled=True)
llm2 = ScriptedLLM([
    call_ping(0),
    {"content": "**结论：序列号为 `4080A8654321`。** 依据：USB 表返回 SN=4080A8654321。"},
])
ag2.call = lambda name, args, quiet=False, on_event=None, log_it=True: \
    "DEVICE_SN=4080A8654321"
ag2._llm = lambda: llm2
events2 = []
text2, _ = ag2.run("USB 序列号是多少？", max_turns=6, tool_budget=10,
                   renew_budget=0, on_event=events2.append)
check("值确实来自工具返回时，结论照常放行",
      not any(e.get("type") == "gate" for e in events2), text2[:100])
ag.close()
ag2.close()

# ---------------------------------------------------------------- 7) 熔断半开
ag = make_agent(enabled=True)
llm = ScriptedLLM([call_ping(i) for i in range(10)] + [{"content": "收尾"}])
ag.call = lambda name, args, quiet=False, on_event=None: "【执行失败】模拟工具故障"
ag._llm = lambda: llm
text, _ = ag.run("随便查点东西", max_turns=10, tool_budget=30, renew_budget=0)
saw_missing = any("ping" not in names for names in llm.tools)
saw_back = any("ping" in names for names in llm.tools[2:])
check("连续失败 3 次 → 熔断（工具从清单消失）", saw_missing,
      f"每轮工具清单含 ping：{[('ping' in n) for n in llm.tools]}")
check("冷却 2 轮后自动放行（半开），不是一刀切死", saw_back,
      f"每轮工具清单含 ping：{[('ping' in n) for n in llm.tools]}")
ag.close()

print(f"\n合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
