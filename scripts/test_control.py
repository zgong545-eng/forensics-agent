"""验证运行中干预：中断（RunControl.stop）与插话（RunControl.push）。

不连 MCP、不连真模型：假模型按脚本演戏，必要时在工具调用里现场触发
中断 / 插话，模拟「用户点击」发生在循环进行中的那一刻。

用法：python scripts/test_control.py
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

from agent import ForensicsAgent, RunControl, Session  # noqa: E402


class ScriptedLLM:
    """按脚本返回；脚本用尽就给最终答复。"""

    def __init__(self, script, tail="（收口总结）"):
        self.script = list(script)
        self.tail = tail
        self.calls = 0
        self.saw_tail = False
        self.seen_user_after = []      # 记录收口前最后一条 user 消息（验证插话有没有进去）

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        if not tools:
            self.saw_tail = True
            return {"content": self.tail}
        self.seen_user_after = [m.get("content") for m in messages
                                if m.get("role") == "user"]
        if self.script:
            return self.script.pop(0)
        return {"content": "正常收口"}


def make_agent(llm):
    cfg = {"mcpServers": {}, "skills": {"roots": [], "allow_commands": []},
           "agent": {}, "tool_timeout": 5}
    a = ForensicsAgent(cfg, Session(enabled=False), verbose=False)
    a._llm = lambda: llm
    return a


def tc(name, args, i=0):
    return {"content": "",
            "tool_calls": [{"id": f"call_{i}", "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args, ensure_ascii=False)}}]}


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("[OK]  " if ok else "[XX]  ") + name + (("  — " + detail) if detail else ""))


# ---------------------------------------------------------------- 1) 中断：轮首检查
def t_stop_before_turn():
    llm = ScriptedLLM([
        tc("sys_tool_list", {}, 0),
        tc("sys_tool_list", {}, 1),      # 不该跑到这里
    ])
    a = make_agent(llm)
    a.call = lambda name, args, quiet=False, on_event=None, **kw: "假工具结果"
    ctl = RunControl()

    def emit(ev):
        if ev.get("type") == "turn" and ev.get("n") == 1:
            ctl.stop()                    # 第 1 轮刚开始就点中断

    answer, hist = a.run("测试中断", max_turns=10, on_event=emit, control=ctl)
    check("中断：轮首检查点生效", llm.calls == 1,
          f"模型只被调了 {llm.calls} 次（工具轮），没有继续下一轮")
    check("中断：不调模型收口", not llm.saw_tail,
          "省掉了收口那次请求")
    check("中断：答复里写明已中断", "已中断" in answer, answer[:40])
    check("中断：历史仍可续聊", isinstance(hist, list) and len(hist) >= 2,
          f"{len(hist)} 条消息")


# ---------------------------------------------------------------- 2) 中断：工具之间
def t_stop_between_tools():
    llm = ScriptedLLM([
        {"content": "",
         "tool_calls": [
             {"id": "c0", "type": "function",
              "function": {"name": "sys_tool_list", "arguments": "{}"}},
             {"id": "c1", "type": "function",
              "function": {"name": "sys_tool_list", "arguments": "{}"}},
             {"id": "c2", "type": "function",
              "function": {"name": "sys_tool_list", "arguments": "{}"}},
         ]},
    ])
    a = make_agent(llm)
    ran = []
    ctl = RunControl()

    def fake_call(name, args, quiet=False, on_event=None, **kw):
        ran.append(name)
        ctl.stop()                       # 第一个工具跑完用户就点了中断
        return "结果"

    a.call = fake_call
    answer, hist = a.run("测试中途中断", max_turns=10, control=ctl)

    check("中断：工具循环内只跑了一次", len(ran) == 1, f"实际执行 {len(ran)} 次")
    # 关键：assistant 声明了 3 个 tool_calls，必须 3 条 tool 响应都补齐，否则下轮 400
    tool_msgs = [m for m in hist if m.get("role") == "tool"]
    check("中断：剩余 tool_calls 补齐响应", len(tool_msgs) == 3,
          f"{len(tool_msgs)}/3 条 tool 消息（少一条下次请求就 400）")
    check("中断：补的响应写明已中断",
          all("已中断" in m["content"] or m["content"] == "结果" for m in tool_msgs))


# ---------------------------------------------------------------- 3) 插话：下一轮生效
def t_inject_next_turn():
    llm = ScriptedLLM([
        tc("sys_tool_list", {}, 0),
       {"content": "好，改查微信"},
    ])
    a = make_agent(llm)
    ctl = RunControl()
    turn = {"n": 0}

    def fake_call(name, args, quiet=False, on_event=None, **kw):
        turn["n"] += 1
        if turn["n"] == 1:
            ctl.push("别查 tool_list 了，直接去查微信记录")
        return "工具结果"

    a.call = fake_call
    events = []
    answer, hist = a.run("测试插话", max_turns=10, control=ctl,
                         on_event=events.append)

    inj_ev = [e for e in events if e.get("type") == "inject"]
    check("插话：后端发出 inject 事件", len(inj_ev) == 1,
          f"{len(inj_ev)} 个 inject 事件")
    injected = [m for m in hist if m.get("role") == "user"
                and "别查 tool_list" in str(m.get("content"))]
    check("插话：作为 user 消息进入上下文", len(injected) == 1)
    check("插话：带「调整方向」提示语",
          bool(injected) and "调整方向" in injected[0]["content"],
          str(injected[0]["content"])[:60] if injected else "")
    check("插话：答复转为模型的新方向", "微信" in answer, answer[:40])


# ---------------------------------------------------------------- 4) 插话：多条排队
def t_inject_multi():
    llm = ScriptedLLM([
        tc("sys_tool_list", {}, 0),
        toc := tc("sys_tool_list", {}, 1),
        {"content": "都记下了"},
    ])
    a = make_agent(llm)
    ctl = RunControl()
    n = {"i": 0}

    def fake_call(name, args, quiet=False, on_event=None, **kw):
        n["i"] += 1
        if n["i"] == 1:
            ctl.push("第一条引导")
            ctl.push("第二条引导")
        return "结果"

    a.call = fake_call
    answer, hist = a.run("测试多条插话", max_turns=10, control=ctl)
    got = [m for m in hist if m.get("role") == "user" and "引导" in str(m.get("content"))]
    check("插话：一次取走队列里全部消息", len(got) == 2, f"{len(got)} 条")


# ---------------------------------------------------------------- 5) 插话不影响消息序列
def t_inject_sequence_valid():
    """插话必须落在「上一条 assistant 的 tool 响应都齐了」之后。"""
    llm = ScriptedLLM([
        tc("sys_tool_list", {}, 0),
        {"content": "结束"},
    ])
    a = make_agent(llm)
    ctl = RunControl()
    ctl.push("一开始就插一句")

    def fake_call(name, args, quiet=False, on_event=None, **kw):
        return "结果"

    a.call = fake_call
    answer, hist = a.run("测试序列", max_turns=5, control=ctl)

    ok = True
    why = ""
    for i, m in enumerate(hist):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc_["id"] for tc_ in m["tool_calls"]}
            j = i + 1
            got = set()
            while j < len(hist) and hist[j].get("role") == "tool":
                got.add(hist[j].get("tool_call_id"))
                j += 1
            if not ids.issubset(got):
                ok = False
                why = f"第 {i} 条 assistant 的 tool_calls 缺响应"
                break
    check("插话：消息序列合法（无孤儿 tool / 缺响应）", ok, why)


# ---------------------------------------------------------------- 6) 收口阶段插话
def t_inject_at_closing():
    """用户是在模型已经开始写总结时才点的发送——这时候没有「下一轮」了，
    收口请求前必须再取一次队列，否则这句话永远躺在队列里。"""
    llm = ScriptedLLM([tc("sys_tool_list", {}, 0)],
                      tail="按你的要求重写：只讲结论")
    a = make_agent(llm)
    ctl = RunControl()

    def fake_call(name, args, quiet=False, on_event=None, **kw):
        return "结果"

    a.call = fake_call
    # max_turns=1：这一轮工具跑完就退出循环，直接进收口
    events = []
    answer, hist = a.run("测试收口插话", max_turns=1, control=ctl,
                         on_event=events.append)
    # 循环已经退出（额度用尽），此时才插话——模拟「慢半拍」
    ctl.push("总结里只要结论，别贴过程")

    # 再跑一次同样的流程，但这次在工具调用时就插话，保证踩到收口前的 drain
    ctl2 = RunControl()

    def fake_call2(name, args, quiet=False, on_event=None, **kw):
        ctl2.push("总结里只要结论，别贴过程")
        return "结果"

    llm2 = ScriptedLLM([tc("sys_tool_list", {}, 0)], tail="只讲结论")
    a2 = make_agent(llm2)
    a2.call = fake_call2
    ev2 = []
    answer2, hist2 = a2.run("测试收口插话2", max_turns=1, control=ctl2,
                            on_event=ev2.append)

    inj = [e for e in ev2 if e.get("type") == "inject"]
    in_ctx = [m for m in hist2 if m.get("role") == "user"
              and "只要结论" in str(m.get("content"))]
    check("收口插话：退出循环后仍被接住", len(inj) == 1, f"{len(inj)} 个 inject 事件")
    check("收口插话：进入了收口请求的上下文", len(in_ctx) == 1,
          f"{len(in_ctx)} 条 user 消息")


# ---------------------------------------------------------------- 7) 无 control 时行为不变
def t_no_control():
    llm = ScriptedLLM([tc("sys_tool_list", {}, 0), {"content": "正常"}])
    a = make_agent(llm)
    a.call = lambda name, args, quiet=False, on_event=None, **kw: "结果"
    answer, hist = a.run("普通一轮", max_turns=5)
    check("无 control：照常跑完", answer.strip() == "正常", answer[:20])


if __name__ == "__main__":
    t_stop_before_turn()
    t_stop_between_tools()
    t_inject_next_turn()
    t_inject_multi()
    t_inject_sequence_valid()
    t_inject_at_closing()
    t_no_control()
    print("\n" + "=" * 56)
    print(f"通过 {sum(RESULTS)}/{len(RESULTS)}")
    sys.exit(0 if all(RESULTS) else 1)
