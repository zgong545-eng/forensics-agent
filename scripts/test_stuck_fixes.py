"""锁住 2026-10-30 用户实测暴露的三个「跑不出题」的死因。

现场（同一台机器、同一个案件、MCP 正常在线）：

  ① 「该计算机中嫌疑人使用邮箱软件登录的邮箱账号是什么？」——
     模型三次收到 `sys_file_scan` 的「该工具只接受单个文件，不接受目录」，
     可它当时其实已经知道要去的目录是 `AppData\\Roaming\\Foxmail7`。
     → 目录能力必须由工具吃掉，不能逼模型一个文件一个文件猜。

  ② 「ASR 模型共有几个？」——
     `query_task_status` 连续 5 次返回同样的 progress 就被熔断，
     可「任务还在跑」本来就会返回一样的东西；熔断后它连等任务的手段都没了。
     → 轮询类工具不能因为「结果没变」被摘掉，只能催它换姿势（阻塞等待 / 先查别的）。

  ③ 第 27 轮直接崩在 `TypeError: 'NoneType' object is not subscriptable`。
     根因：模型接口回了空体 / null，`payload["choices"]` 抛的 TypeError
     不在 `except (KeyError, IndexError)` 里，整轮对话报废。
     → 解析必须认下「返回不是 JSON 对象」这件事，转成可读的 LlmError。

另外锁第四件事：结尾「要我走这条路吗？」这种把决定权推回给用户的收口，
必须被打回——用户已经说过「你去查，我只要答案」。

用法：python scripts/test_stuck_fixes.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试隔离：经验沉淀改道到临时目录，绝不往真实的 prompts/lessons.md、
# logs/memory/qa.jsonl 里写「D 盘卷标是什么」这种假问题。
import os as _os, tempfile as _tf, shutil as _sh
_LEARN_TMP = str(ROOT / "logs" / "_test")
_os.makedirs(_LEARN_TMP, exist_ok=True)
_os.environ["LEARN_DIR"] = _LEARN_TMP

# 本轮用户要求「不许往 C 盘写任何东西」，临时目录一律落在项目盘
_TMP = ROOT / ".tmp"
_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(_TMP)

import agent as A                                        # noqa: E402
from agent import (ForensicsAgent, LlmClient, LlmError,  # noqa: E402
                   Session, scan_tree_stream)

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
        self.seen = []
        self.tools = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        self.tools.append([(t.get("function") or {}).get("name") for t in (tools or [])])
        return self.script.pop(0) if self.script else {"content": "（兜底）"}


def make_agent():
    cfg = {
        "mcpServers": {"fake": {"command": PY, "args": [FAKE],
                                "enabled": True, "timeout": 20}},
        "skills": {"roots": [str(ROOT / "skills")], "allow_commands": ["ges"]},
        "agent": {}, "tool_timeout": 20,
    }
    return ForensicsAgent(cfg, Session(enabled=False), verbose=False,
                          cfg_path=ROOT / "config.json")


def call(name, args="{}", i=0):
    return {"content": "", "tool_calls": [{
        "id": f"c{i}", "type": "function",
        "function": {"name": name, "arguments": args}}]}


# ==================================================== ① 目录扫描（福尔摩斯点：Foxmail）
d = Path(tempfile.mkdtemp(prefix="tree_"))
acct = d / "Foxmail 7.2" / "Storage" / "somebadthings2026@163.com" / "Accounts"
acct.mkdir(parents=True)
(acct / "Account.cfg").write_text("Account=somebadthings2026@163.com\n", encoding="utf-8")
(acct / "Account.htb").write_bytes(b"\x00" * 2048)
(d / "Foxmail 7.2" / "Storage" / "notes.txt").write_text(
    "无关内容，没有任何账号", encoding="utf-8")

rep = scan_tree_stream(d, ["@163.com"], include="*.cfg,*.ini")
check("目录扫描：按后缀过滤后能扫到账号文件", "Account.cfg" in rep, rep[:500])
check("目录扫描：命中原文里带得出账号",
      "somebadthings2026@163.com" in rep, rep[:600])
check("目录扫描：非目标后缀被 include 挡掉",
      "notes.txt" not in rep, rep[:600])

rep_all = scan_tree_stream(d, ["@163.com"])
check("目录扫描：不填 include 时递归覆盖全部文件（候选数含未命中的）",
      "候选文件 3 个" in rep_all and "Account.cfg" in rep_all, rep_all[:400])

rep_none = scan_tree_stream(d, ["jelly@tuojieinfo.com"])
check("目录扫描：0 命中明确写「都真读过」而不是「没扫」",
      "命中：0" in rep_none and "真读过" in rep_none, rep_none[:400])

rep_flat = scan_tree_stream(d, ["@163.com"], recursive=False)
check("目录扫描：recursive=false 只看本层",
      "Account.cfg" not in rep_flat, rep_flat[:400])

# ==================================================== ② 轮询工具不该被「结果没变」熔断
ag = make_agent()
llm = ScriptedLLM([call("sys_server_status", "{}", i) for i in range(5)]
                  + [{"content": "结论：状态已确认。"}])
ag._llm = lambda: llm
events = []
text, _ = ag.run("看看服务器状态", max_turns=10, tool_budget=30,
                 renew_budget=0, on_event=events.append)
fused = [e for e in events if e.get("type") == "tool_fused"]
check("轮询工具（sys_server_status）连续 5 次同样结果→不熔断",
      not fused, [e.get("text") for e in fused])
joined = "\n".join(str(m.get("content")) for m in llm.seen[-1])
check("轮询空转时给出「改用 wait_task_completion 阻塞等」的指令",
      "wait_task_completion" in joined and "别再连续轮询" in joined, joined[-500:])
ag.close()

# 非轮询工具保持原样：3 次同样结果就熔断
ag = make_agent()
llm = ScriptedLLM([call("fake_ping", "{}", i) for i in range(3)]
                  + [{"content": "结论：换路了。"}])
ag._llm = lambda: llm
events = []
text, _ = ag.run("随便查", max_turns=8, tool_budget=20, renew_budget=0,
                 on_event=events.append)
fused = [e for e in events if e.get("type") == "tool_fused"]
check("非轮询工具连续 3 次同样结果→照旧熔断",
      len(fused) == 1 and "fake_ping" in str(fused[0].get("name")), fused)
ag.close()


# ==================================================== ③ 模型返回 null 不许把对话炸掉
class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_urlopen = A.urllib.request.urlopen
for body, label in ((b"null", "null"), (b"", "空体"),
                    (b'{"error":{"message":"boom"}}', "error 体")):
    A.urllib.request.urlopen = lambda *a, _b=body, **k: FakeResp(_b)
    try:
        LlmClient("http://127.0.0.1:1/v1", "k", "m", max_retries=0)._post({})
        check(f"模型返回 {label} → 应抛 LlmError", False, "没抛异常")
    except LlmError as e:
        check(f"模型返回 {label} → 抛可读的 LlmError（不再是 TypeError）",
              "TypeError" not in str(e), str(e)[:160])
    except TypeError as e:
        check(f"模型返回 {label} → 抛可读的 LlmError（不再是 TypeError）",
              False, f"仍然 TypeError: {e}")
    except Exception as e:                                # noqa: BLE001
        check(f"模型返回 {label} → 抛可读的 LlmError（不再是 TypeError）",
              False, f"{type(e).__name__}: {e}")
A.urllib.request.urlopen = _real_urlopen

# ==================================================== ④ 不许把决定权推回给用户
ag = make_agent()
llm = ScriptedLLM([
    {"content": "已确认：案件已打开、检材已挂载。\n需要我现在就按第 2 条去挂载视图里逐个定位这些配置文件吗？"},
    {"content": "结论：账号是 somebadthings2026@163.com。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机中嫌疑人使用邮箱软件登录的邮箱账号是什么？",
                 max_turns=8, tool_budget=20, renew_budget=0, on_event=events.append)
gates = [e for e in events if e.get("type") == "gate"]
check("结尾「要我…吗？」→ 被打回（不再拿提问当免责）",
      len(gates) == 1 and "许可" in str(gates[0].get("text")), gates)
pushed = "\n".join(str(m.get("content")) for m in llm.seen[1])
check("打回时点明「用户要的是答案，不是选择题」",
      "不是选择题" in pushed and "别再问" in pushed, pushed[-400:])
check("打回后允许正常收口", "somebadthings2026@163.com" in text, text[:120])
ag.close()

# ==================================================== ⑤ 额度耗尽时必须逼出文字
# 现场：第 4 轮就用 mode=dir 拿到了 3 个模型目录，但后面被 D-Bus 超时耗光 14 轮，
# 收口那次请求返回空 content，用户看到「共跑 14 轮、没能生成小结」——
# 明明答案早就查到了。所以：收口要明确禁止调工具，空则重试，再空就把原始结果摆出来。
check("兜底摘要：把真实工具返回摆出来（不是「没能生成小结」）",
      "实际拿到的工具返回" in A.digest_evidence(["任务返回 count=3"]),
      A.digest_evidence(["任务返回 count=3"]))
check("兜底摘要：没有证据时返回空，不伪造内容", A.digest_evidence([]) == "")

ag = make_agent()
llm = ScriptedLLM([call("sys_server_status", "{}", 80),
                   call("sys_server_status", "{}", 81),
                   {"content": ""}, {"content": ""}])
ag._llm = lambda: llm
text, _ = ag.run("看下状态", max_turns=2, tool_budget=10,
                 renew_budget=0, on_event=lambda e: None)
closing = "\n".join(str(m.get("content")) for m in (llm.seen[-1] or []))
check("收口请求里写明「不会再给你任何工具」",
      "不会再给你任何工具" in closing, closing[-400:])
check("收口请求要求直接给答案值、禁止再说「需要继续查」",
      "不要再说「需要继续查」" in closing, closing[-400:])
check("收口时工具清单是空的（模型没法再调）",
      not llm.tools[-1], llm.tools[-1])
check("模型两次都写不出字时，正文回落到原始工具结果",
      "实际拿到的工具返回" in text or "状态" in text, text[:200])
ag.close()

# ==================================================== 汇总
ok = sum(results)
print(f"\n合计 {ok}/{len(results)} 通过")
sys.exit(0 if ok == len(results) else 1)
