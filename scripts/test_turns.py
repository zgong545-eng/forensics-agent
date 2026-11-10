"""验证 run() 的退出路径与额度续期。

不连 MCP、不连真模型：把 _llm() 换成一个按脚本演戏的假客户端，
必要时连 call() 也换掉，用来精确控制「这次调用算不算有进展」。
用法：python scripts/test_turns.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试隔离：经验沉淀改道到临时目录，别把假问题写进真实经验库。
import os as _os, tempfile as _tf
_LEARN_TMP = str(Path(__file__).resolve().parent.parent / "logs" / "_test")
_os.makedirs(_LEARN_TMP, exist_ok=True)
_os.environ["LEARN_DIR"] = _LEARN_TMP

from agent import ForensicsAgent, Session  # noqa: E402


class ScriptedLLM:
    """按预设脚本返回；无脚本可用时返回最终答复。"""

    def __init__(self, script, tail="（收口总结）已经查到的部分如下…"):
        self.script = list(script)
        self.tail = tail
        self.calls = 0
        self.saw_tail = False

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        if not tools:                      # 收口请求：不带 tools
            self.saw_tail = True
            return {"content": self.tail}
        if self.script:
            return self.script.pop(0)
        return {"content": "正常收口"}


def make_agent(llm):
    cfg = {"mcpServers": {}, "skills": {"roots": [], "allow_commands": []},
           "agent": {}, "tool_timeout": 5}
    a = ForensicsAgent(cfg, Session(enabled=False), verbose=False)
    a._llm = lambda: llm            # 绕过真模型
    return a


def tc(name, args, i=0):
    """造一个 tool_call。"""
    return {"content": "",
            "tool_calls": [{"id": f"call_{i}", "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args, ensure_ascii=False)}}]}


def tcm(pairs):
    """一轮里并行发多个 tool_call。"""
    return {"content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function",
         "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
        for i, (n, a) in enumerate(pairs)]}


def case(title, script, expect_stop, max_turns=40, tool_budget=120,
         renew_budget=3, fake_call=None, expect_event=None, expect_no_stop=False):
    llm = ScriptedLLM(script)
    ag = make_agent(llm)
    if fake_call is not None:
        ag.call = fake_call
    events = []
    text, _ = ag.run("测试", max_turns=max_turns, tool_budget=tool_budget,
                     renew_budget=renew_budget, on_event=events.append)
    renews = sum(1 for e in events if e.get("type") == "renew")
    fused = [e.get("name") for e in events if e.get("type") == "tool_fused"]
    ok = (expect_stop in text) and (expect_event is None
                                    or any(e.get("type") == expect_event for e in events))
    if expect_no_stop:
        # 「熔断」不等于「整轮停下」：这条要求答复里不出现停止横幅
        ok = ok and ("本次在" not in text)
    print(f"\n[{ '通过' if ok else '失败' }] {title}")
    print(f"   模型调用 {llm.calls} 次 · 续期 {renews} 次 · 熔断 {fused} · "
          f"收口请求 {'有' if llm.saw_tail else '无'}")
    print("   答复片段：" + text.replace("\n", " ")[:160])
    return ok


results = []

# 1) 正常：第一轮就出文字，不调工具
results.append(case(
    "正常答题（第一轮直接给结论）",
    [{"content": "这是直接答案。"}],
    expect_stop="这是直接答案",
))

# 2) 同一个工具 + 完全一样的参数磕 10 次 → 熔断这个工具，任务继续，不整轮中断
results.append(case(
    "重复调用同一工具同一参数 → 熔断该工具（不整轮停下）",
    [tc("sys_skill_list", {}, i) for i in range(10)],
    expect_stop="已经查到的部分",
    expect_event="tool_fused",
    max_turns=40,
))

# 3) 连调一个不存在的工具 → 同样只熔断，不以「连续报错」收场
results.append(case(
    "连续报错 → 熔断该工具，继续给出结论",
    [tc("no_such_tool_xyz", {"a": 1}, i) for i in range(10)],
    expect_stop="已经查到的部分",
    expect_event="tool_fused",
))

# 4) 额度用尽但仍在推进 → 自动续期，不中断
_prog = {"n": 0}


def _progressing(name, args, quiet=False, on_event=None, **kw):
    _prog["n"] += 1
    return f"第 {_prog['n']} 条不同的结果，信息量在增加"


results.append(case(
    "额度用尽但一直在推进 → 自动续期，不中断",
    [tc("sys_skill_list", {"q": f"x{i}"}, i) for i in range(30)],
    expect_stop="已自动续期",
    max_turns=2, tool_budget=2, renew_budget=2,
    fake_call=_progressing,
))

# 5) 额度用尽且本轮没产出新信息 → 不续期，直接收口
results.append(case(
    "额度用尽且本轮无新信息 → 不续期，直接收口",
    [tcm([("sys_skill_list", {"q": "a"}), ("sys_skill_list", {"q": "b"})])],
    expect_stop="没有产出新信息",
    max_turns=1, tool_budget=2, renew_budget=3,
    fake_call=lambda name, args, quiet=False, on_event=None, **kw: "同一份结果",
))

# 6) 参数一路换、答案还是那一份 → 结果指纹判定，熔断这个数据源。
#    熔断是「半开」的：关 2 轮后放行一次给模型换参数重试，
#    所以这里不该出现「本次在…处停下」，而是熔断过、最后正常收口。
results.append(case(
    "换着参数问，结果一模一样 → 熔断该工具（不整轮停下，冷却后复活）",
    [tc("sys_skill_list", {"vendor": f"prov{i}"}, i) for i in range(8)]
    + [{"content": "查完了，结论如下。"}],
    expect_stop="查完了",
    expect_event="tool_fused",
    expect_no_stop=True,
    max_turns=40,
))

print("\n" + "=" * 56)

# 5) _trim 不能砍出孤儿消息（否则接口直接 400）
tr = ForensicsAgent({"mcpServers": {}, "skills": {"roots": []}},
                    Session(enabled=False), verbose=False)
msgs = [
    {"role": "system", "content": "S"},
    {"role": "user", "content": "U"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
    {"role": "tool", "tool_call_id": "c0", "content": "x" * 200},   # ← tail 起点
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
    {"role": "tool", "tool_call_id": "c1", "content": "x" * 200},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
    {"role": "tool", "tool_call_id": "c2", "content": "x" * 200},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c3"}]},
]
tr._trim(msgs, limit_chars=10, keep_pairs=3)
roles = [m["role"] for m in msgs]
orphan_head = bool(msgs) and msgs[0].get("role") == "tool"
orphan_tail = bool(msgs) and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls")
ok5 = not orphan_head and not orphan_tail
print(f"\n[{ '通过' if ok5 else '失败' }] _trim 不产生孤儿消息")
print(f"   裁剪后 {len(msgs)} 条: {roles}")
results.append(ok5)

# 6) _trim 被砍掉的中间段必须留下摘要，否则模型会「失忆」重查
digest_msg = next((m for m in msgs if "早期步骤摘要" in str(m.get("content") or "")), None)
ok6 = digest_msg is not None
print(f"\n[{ '通过' if ok6 else '失败' }] _trim 保留早期步骤摘要（防失忆重查）")
if digest_msg:
    print("   摘要片段：" + digest_msg["content"].replace("\n", " ")[:150])
results.append(ok6)

print("\n" + "=" * 56)

# 7) 相同调用第 2 次：不再真跑，但也不能直接掐——只提示，让模型自己换路子
#    （特意避开 ges status 这类轮询命令，它们本来就该允许重复）
_cnt = {"n": 0}


def _counted(name, args, quiet=False, on_event=None, **kw):
    _cnt["n"] += 1
    return "第一次的真实结果"


_DUP = {"name": "ges-case-management", "command": "ges evidence ls"}
_llm7 = ScriptedLLM([tc("sys_skill_exec", _DUP, 0),
                     tc("sys_skill_exec", dict(_DUP), 1),
                     {"content": "换个思路：直接用 MCP 工具查案件。"}])
_ag7 = make_agent(_llm7)
_ag7.call = _counted
_text7, _ = _ag7.run("测试", max_turns=40, tool_budget=120, renew_budget=0)
ok7 = (_cnt["n"] == 1) and ("换个思路" in _text7)
print(f"\n[{ '通过' if ok7 else '失败' }] 相同调用第 2 次不重复执行，且进程继续")
print(f"   真实执行 {_cnt['n']} 次（应为 1）· 得到答复：{_text7[:60]}")
results.append(ok7)

# 8) 提前停止时，assistant 声明的 tool_calls 必须全部有 tool 响应，否则收口 400
_llm8 = ScriptedLLM([tcm([("sys_skill_exec", {"name": "a", "command": f"c{i}"})
                          for i in range(3)]) for _ in range(6)])
_ag8 = make_agent(_llm8)
_ag8.call = lambda name, args, quiet=False, on_event=None, **kw: "【命令失败 exit=1】没成"
_ag8.run("测试", max_turns=40, tool_budget=120, renew_budget=0)
print(f"\n[{ '通过' if _llm8.saw_tail else '失败' }] 早停时补齐所有 tool 响应（收口请求没被 400）")
results.append(_llm8.saw_tail)

# 9) 轮询类工具（wait_*/status）本来就该重复调，不能被「同参数」短路
_poll = {"n": 0}


def _polling(name, args, quiet=False, on_event=None, **kw):
    _poll["n"] += 1
    return f"解析进度 {_poll['n'] * 10}%"          # 每次结果都不同 = 真在推进


_llm9 = ScriptedLLM([tc("wait_task_completion", {"taskId": "t1"}, i) for i in range(4)]
                    + [{"content": "解析完成，结论如下。"}])
_ag9 = make_agent(_llm9)
_ag9.call = _polling
_t9, _ = _ag9.run("测试", max_turns=40, tool_budget=120, renew_budget=0)
ok9 = (_poll["n"] == 4) and ("结论如下" in _t9)
print(f"\n[{ '通过' if ok9 else '失败' }] 轮询类工具（wait_*/*status）不被重复短路")
print(f"   真实执行 {_poll['n']} 次（应为 4）")
results.append(ok9)

print("\n" + "=" * 56)
print(f"通过 {sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
