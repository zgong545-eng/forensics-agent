"""验证「问你能干什么」不会被当成取证任务办掉。

现场事故（2026-10-30 19:2x，用户亲测并骂了）：
  用户：「你能干什么？」
  模型答了一份能力清单，其中一句是：
    「需要密码（BitLocker、手机锁屏、加密文档）」
  ——这是在讲**能力边界**。
  结果被裸关键词 `密码` 判成「无证据硬断言」，打回重写。模型于是交出：
    「结论：未取得 / 关于「密码」一词 / 已确认·未确认 表格」
  用户在问你是谁，收到的却是一张检讨书。

根因两条，本文件逐条锁：
  1. 硬断言的判定写成了「命中词表」，正确条件是「命中词表 **且** 给出了具体值」
     （is_hard_claim）——没有具体值，压根没有编造的余地。
  2. 元问题（问的是"你"，不是某份检材）不该走取证闸门
     （is_meta_ask + 元问题白卷闸）。

用法：python scripts/test_meta_ask.py
"""
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

from agent import (ForensicsAgent, Session, is_hard_claim,  # noqa: E402
                   is_meta_ask)

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

    def chat(self, messages, tools=None, tool_choice="auto"):
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


# ---------------------------------------------------------------- 1) 元问题识别
META = ["你能干什么", "你能做什么", "你能帮我做些什么", "你能干嘛",
        "你都会些什么", "你有什么功能", "支持哪些功能", "怎么用",
        "如何使用这个系统", "你是谁", "介绍下你自己", "这个 agent 能干什么",
        "help", "？"]
for q in META:
    check(f"识别为元问题：{q}", is_meta_ask(q), is_meta_ask(q))

REAL = ["该计算机的开机密码是多少", "帮我查一下这个案子的邮箱账号",
        "分析下 E:/case/20261012143420 这个检材",
        "你能查一下这个镜像里装了哪些软件吗",
        "帮我分析这个案子，你能干什么都行，先把浏览器记录拉出来"]
for q in REAL:
    check(f"不误判为元问题：{q[:20]}", not is_meta_ask(q), is_meta_ask(q))

# ---------------------------------------------------------------- 2) 硬断言要「有值」
NOCLAIM = [
    "需要密码（BitLocker、手机锁屏、加密文档）",
    "我可以读浏览器记录，也能从聊天数据库里找账号信息",
    "卷标、序列号这类信息需要先挂载才能拿到",
    "IP 地址和 MAC 地址会在网络连接表里出现",
]
for t in NOCLAIM:
    check(f"只是能力/流程说明，不算硬断言：{t[:16]}…", not is_hard_claim(t), t)

CLAIM = [
    "密码是 `123456`",
    "登录密码 260919",
    "该账户密码为 somebadthings2026@163.com",
    "序列号 `4080A8654321`",
    "主机 IP 地址为 103.87.9.67",
]
for t in CLAIM:
    check(f"给了具体值，算硬断言：{t[:16]}…", is_hard_claim(t), t)

# ---------------------------------------------------------------- 3) 端到端：能力清单照常交付
CAP = ("我能做的：\n"
       "1. 直接读文件——本机磁盘、火眼挂载视图 `C:/hlnet/...`、导出目录；\n"
       "2. 内置工具：列目录、读文本、分块扫关键词、查 FS 索引；\n"
       "3. 还能接 MCP（可选），用来查案件、提解析任务、挂载设备；\n"
       "4. 有些场景需要你提供密码（BitLocker、手机锁屏、加密文档）——这只是能力边界。\n"
       "把挂载路径或案件号发我，我直接开工。")

ag = make_agent(enabled=True)
llm = ScriptedLLM([{"content": CAP}])
ag._llm = lambda: llm
events = []
text, _ = ag.run("你能干什么", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
gates = [e for e in events if e.get("type") == "gate"]
check("问「你能干什么」→ 不触发任何闸门", not gates, gates)
check("答复原文交付，没有被改成白卷",
      text.strip().startswith("我能做的") and "未取得" not in text, text[:120])
check("含「密码」一词的能力说明不再被拦",
      "需要你提供密码" in text, text[:200])
ag.close()

# ---------------------------------------------------------------- 4) 端到端：白卷被打回重写
ag = make_agent(enabled=False)
llm = ScriptedLLM([
    {"content": "**结论：未取得。** 没有检材路径，无法作答。"},
    {"content": CAP},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("你能干什么", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
gates = [e for e in events if e.get("type") == "gate"]
check("元问题答成「未取得」→ 被打回", bool(gates), [e.get("text") for e in gates])
gate_msg = ""
for m in (llm.seen[1] if len(llm.seen) > 1 else []):
    if "元问题" in str(m.get("content") or ""):
        gate_msg = str(m.get("content"))
check("打回说明里点明「用户在问你能干什么，不是在查检材」",
      "问**你能干什么" in gate_msg or "问你能干什么" in gate_msg, gate_msg[:200])
check("重写后交付的是能力清单，不是「未取得」",
      "我能做的" in text and "未取得" not in text, text[:120])
ag.close()

# ---------------------------------------------------------------- 5) 真取证问题仍受门禁
ag = make_agent(enabled=False)   # 没有任何工具：不许给具体值
llm = ScriptedLLM([
    {"content": "**结论：该计算机的开机密码为 `123456`。**"},
    {"content": "**结论：未取得。** 没有可用的取证工具。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("该计算机的开机密码是多少", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
check("取证问题没有证据时，编的密码仍被拦下",
      any(e.get("type") == "gate" for e in events) and "123456" not in text,
      f"events={[e.get('type') for e in events]} text={text[:80]}")
ag.close()

# ---------------------------------------------------------------- 6) 身份：只答「电子数据取证分析师」
# 现场事故（2026-11-02 14:40）：问「你是什么」，答「运行在 WorkBuddy 里，
# 底层模型名称和版本我这边看不到，也没法自证」——两句都是编的。
# 用户随后明确要求：**只回答身份，不要回答模型**。所以提示词里既不提模型名，
# 也明令禁止它主动报版本/接口/部署方式。
_id_cfg = {
    "mcpServers": {}, "skills": {"roots": [], "allow_commands": []},
    "llm": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "agent": {},
}
_ag = ForensicsAgent(_id_cfg, Session(enabled=False), verbose=False,
                     cfg_path=ROOT / "config.json")
_sp = _ag._system_prompt()
check("身份段要求只答「我是电子数据取证分析师」",
      "你是一名**电子数据取证分析师**" in _sp and "我是电子数据取证分析师。" in _sp)
check("模型名/接口一律不注入提示词",
      "deepseek-chat" not in _sp and "{{MODEL}}" not in _sp and "{{BASE_URL}}" not in _sp)
check("明令禁止报模型名 / 版本 / 部署方式",
      "**不要**报模型名、版本号、接口地址、部署方式" in _sp)
check("WorkBuddy 只作路径判断依据，不许讲给用户",
      "不要讲给用户听" in _sp)
_ag.close()

print(f"\n合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
