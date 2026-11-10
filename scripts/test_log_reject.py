"""验证「答案落盘」与「工具反复失败不再整轮停下」。

三个回归点：
  1. Session 只有第一条能落盘 —— 历史会话里只剩提问，没有答案
  2. 安全闸拦截被算进「连续报错」，三次就整轮停下
  3. 「连续 3 次工具调用都报错」不该是收场方式：失败的工具只熔断，
     任务继续用别的路子走，除非真的没有新信息了

用法：python scripts/test_log_reject.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试隔离：经验沉淀改道到临时目录，别把假问题写进真实经验库。
import os as _os, tempfile as _tf
_LEARN_TMP = str(Path(__file__).resolve().parent.parent / "logs" / "_test")
_os.makedirs(_LEARN_TMP, exist_ok=True)
_os.environ["LEARN_DIR"] = _LEARN_TMP

from agent import (ForensicsAgent, Session, STEER_BLOCKED,  # noqa: E402
                  STEER_FAILED, failure_kind, strip_tool_markup)

results = []


def check(title, ok, detail=""):
    results.append(bool(ok))
    print(f"\n[{'通过' if ok else '失败'}] {title}")
    if detail:
        print("   " + str(detail).replace("\n", " ")[:220])


# ---------------------------------------------------------------- 1) 落盘
tmp = Path(tempfile.mkdtemp()) / "s.jsonl"
s = Session(enabled=True, path=tmp)
s.log("user", text="问一句")
s.log("tool_call", tool="x", args={}, result="r")
s.log("assistant_final", text="答一句")
lines = [l for l in tmp.read_text(encoding="utf-8").splitlines() if l.strip()]
kinds = [json.loads(l)["kind"] for l in lines]
check("同一会话里多条事件都能落盘（历史会话不再只有提问）",
      kinds == ["user", "tool_call", "assistant_final"], kinds)
s.close()

# 懒创建仍然有效：没说话就不建文件
tmp2 = Path(tempfile.mkdtemp()) / "empty.jsonl"
s2 = Session(enabled=True, path=tmp2)
s2.log("agent_ready", servers=[])
check("空会话不建文件（启动噪音不会留垃圾）", not tmp2.exists(),
      f"exists={tmp2.exists()}")
s2.close()

# ---------------------------------------------------------------- 2) 分类
cases = [
    ("【已拒绝】命令「python」是通用解释器", "blocked"),
    ("【已拦截】这条命令看起来会改动数据", "blocked"),
    ("【无法执行】技能依赖的 CLI 在本机都解析不到", "blocked"),
    ("【命令失败 exit=1】ges xxx", "failed"),
    ("【超时】命令执行超过 300s", "failed"),
    ("【工具异常】McpError: 找不到", "failed"),
    ("正常结果，没有报错", ""),
    ("[exit=1] 老格式", ""),
]
bad = [(t, failure_kind(t), want) for t, want in cases if failure_kind(t) != want]
check("拒绝 / 失败 / 正常 三类分得开", not bad, bad or f"{len(cases)} 例全对")

# ---------------------------------------------------------------- 3) DSML
dirty = ('分号被拦截。\n\n<｜｜DSML｜｜ calls>\n'
         '<｜｜DSML｜｜ invoke name="sys_skill_exec">\n'
         '<｜｜DSML｜｜ parameter name="command" string="true">python -c "x"')
check("正文里残留的工具标记会被清掉", strip_tool_markup(dirty).strip() == "分号被拦截。",
      repr(strip_tool_markup(dirty)))


# ---------------------------------------------------------------- 4) 熔断而非停下
class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.seen = []          # 每次请求前的 messages 快照（浅拷贝）
        self.tools = []         # 每次请求下发的工具名清单

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.seen.append(list(messages))
        self.tools.append([(t.get("function") or {}).get("name") for t in (tools or [])])
        if not tools:
            return {"content": "（收口总结）"}
        if self.script:
            return self.script.pop(0)
        return {"content": "正常收口"}


def tc(i, same=False):
    cmd = "python -c 1" if same else f"python -c snippet_{i}"
    return {"content": "", "tool_calls": [{
        "id": f"call_{i}", "type": "function",
        "function": {"name": "sys_skill_exec",
                     "arguments": json.dumps({"name": "ges", "command": cmd})}}]}


def run_case(reply, same=False):
    cfg = {"mcpServers": {}, "skills": {"roots": [], "allow_commands": []},
           "agent": {}, "tool_timeout": 5}
    ag = ForensicsAgent(cfg, Session(enabled=False), verbose=False)
    llm = ScriptedLLM([tc(i, same) for i in range(6)])
    ag._llm = lambda: llm
    ag.call = (lambda name, args, quiet=False, on_event=None: reply(name, args)) \
        if callable(reply) else \
        (lambda name, args, quiet=False, on_event=None: reply)
    text, _ = ag.run("测试", max_turns=10, tool_budget=30, renew_budget=0)
    return ag, llm, text


BLOCK_MSG = "【已拒绝】命令「python」是通用解释器/Shell，本工具不执行这类命令。"

_, llm, text = run_case(BLOCK_MSG)
check("连续被拒不再整轮停下（没有「连续 3 次…处停下」这种收场）",
      "处停下" not in text, text.replace("\n", " ")[-160:])
check("任务在被拒后还在继续跑（不止 3 次模型调用）", llm.calls >= 4, f"模型调用 {llm.calls} 次")
check("熔断后工具清单里真的没有它了",
      any("sys_skill_exec" not in names for names in llm.tools),
      llm.tools)
check("熔断后剩下的工具仍然可用（任务是继续跑，不是被掐死）",
      any(names and "sys_skill_exec" not in names for names in llm.tools),
      f"各轮工具={[len(x) for x in llm.tools]}")
check("停之前确实试过换路子（带了 STEER）",
      any("别再用这条命令" in str(m.get("content") or "") for m in llm.seen[2])
      if len(llm.seen) > 2 else False,
      f"模型调用 {llm.calls} 次")

_, llm2, text2 = run_case("【命令失败 exit=9】ges case list — 找不到案件")
check("真失败也不再以「连续 3 次工具调用都报错」收场",
      ("工具调用都失败" not in text2) and ("处停下" not in text2),
      text2.replace("\n", " ")[-160:])
check("失败第 2 次注入了「换个思路」",
      any("换个思路" in str(m.get("content") or "") for m in llm2.seen[2])
      if len(llm2.seen) > 2 else False,
      f"模型调用 {llm2.calls} 次")
check("失败工具同样被熔断（换了路子而不是硬凿）",
      any("sys_skill_exec" not in names for names in llm2.tools), llm2.tools)

# 正常路径没被误伤：每次返回的内容都不一样（真在推进），不该熔断
_, llm3, text3 = run_case(lambda name, args: f"【正常】拿到 {args.get('command')} 的记录")
check("正常返回不会被判成原地打转或熔断",
      ("工具调用都失败" not in text3) and all("sys_skill_exec" in names for names in llm3.tools),
      text3.replace("\n", " ")[:80])

# 同一条被拒命令反复发（不换写法）→ 同参数重复，同样只熔断
_, llm4, text4 = run_case(BLOCK_MSG, same=True)
check("同参数重复被拒 → 熔断该工具，不整轮停下",
      any("sys_skill_exec" not in names for names in llm4.tools) and ("处停下" not in text4),
      f"各轮工具={[len(x) for x in llm4.tools]}")

# ---------------------------------------------------------------- 5) 接口挂了也要留回答
class BoomLLM:
    def chat(self, messages, tools=None, tool_choice="auto"):
        raise RuntimeError("502 Bad Gateway")


tmp5 = Path(tempfile.mkdtemp()) / "boom.jsonl"
s5 = Session(enabled=True, path=tmp5)
ag5 = ForensicsAgent({"mcpServers": {}, "skills": {"roots": []},
                      "agent": {}, "tool_timeout": 5}, s5, verbose=False)
ag5._llm = lambda: BoomLLM()
text5, _ = ag5.run("测试", max_turns=5, tool_budget=10, renew_budget=0)
s5.close()
kinds5 = [json.loads(l)["kind"] for l in tmp5.read_text(encoding="utf-8").splitlines() if l.strip()]
check("模型接口 500 也不会只剩提问（仍写一条回答）",
      kinds5 == ["user", "assistant_final"] and "502" in text5,
      f"{kinds5} · {text5[:80]}")

print("\n" + "=" * 56)
print(f"通过 {sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
