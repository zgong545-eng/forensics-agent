"""验证「经验沉淀」真的起作用，而且不会污染安全规则。

背景：用户问「他有自己学习的能力吗」→ 答案是没有（不改权重、不改规则、跨会话不记忆）。
于是补了两层**可控、可回退**的学习：

  ① 教训库 `prompts/lessons.md`：被闸门打回 / 交白卷 / 自称没工具 / 工具熔断 /
     用户插话纠正 / 撞墙 —— 自动记一条，下次拼系统提示词带上。
  ② 成功路径 `logs/memory/qa.jsonl`：真的用工具查到东西时记下「问题 → 结论 → 用了哪些工具」，
     下次遇到相似问题先给个参考。

本文件锁四件事：
  · 分类正确、优先级对（用户纠正 > 闸门 > 熔断 > 撞墙）
  · 去重与条数上限（不能无限膨胀）
  · 注入文本必须写明「经验不是证据」
  · 端到端：跑完一轮，文件里真多了一条

用法：python scripts/test_learn.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import learn  # noqa: E402
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
        "llm": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        "agent": {}, "tool_timeout": 20,
    }
    return ForensicsAgent(cfg, Session(enabled=False), verbose=False,
                          cfg_path=ROOT / "config.json")


def call_ping(i=0):
    return {"content": "", "tool_calls": [{
        "id": f"c{i}", "type": "function",
        "function": {"name": "ping", "arguments": "{}"}}]}


TMP = Path(tempfile.mkdtemp(prefix="learn-", dir=str(ROOT / "logs")))

# ---------------------------------------------------------------- 1) 分类与优先级
C = learn.classify
check("用户纠正优先于一切",
      C({"corrects": ["不对，应该看 D 盘"], "gates": 2})[0] == "用户纠正")
check("闸门打回识别为「证据闸」",
      C({"gates": 1, "gate_hint": "密码 123456"})[0] == "证据闸")
check("交白卷识别为「白卷闸」",
      C({"surrender": 1, "blocker": "ges.grep file_too_large"})[0] == "白卷闸")
check("自称没工具识别为「自称没工具」",
      C({"selfcheck": 1})[0] == "自称没工具")
check("熔断识别为「工具熔断」",
      C({"fused": ["ping"]})[0] == "工具熔断")
check("只有撞墙也会被记下",
      C({"blockers": [("ges.grep", "file_too_large")]})[0] == "撞墙")
check("一切正常时不记（不产生噪音）", C({})[0] == "")

# ---------------------------------------------------------------- 2) 写入 / 去重 / 上限
for i in range(learn.LESSON_MAX + 8):
    learn.record_round(TMP, f"第 {i} 个问题", "", {"gates": 1, "gate_hint": f"密码 {i}"})
lp = learn._lessons_path(TMP)
lines = [l for l in lp.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
check(f"条数上限生效（{len(lines)} ≤ {learn.LESSON_MAX}）",
      len(lines) <= learn.LESSON_MAX, len(lines))
check("文件头有「可以随手删」的说明", "可以随手删" in lp.read_text(encoding="utf-8"))

# 同一个坑重复踩，不该刷屏
before = len([l for l in lp.read_text(encoding="utf-8").splitlines() if l.startswith("- ")])
for _ in range(5):
    learn.record_round(TMP, "同题", "", {"gates": 1, "gate_hint": "同一条断言"})
after = len([l for l in lp.read_text(encoding="utf-8").splitlines() if l.startswith("- ")])
check("同类同细节只保留一条", after <= before + 1, f"{before} → {after}")

# ---------------------------------------------------------------- 3) 注入文本
txt = learn.lessons_text(TMP)
check("注入文本带上了教训条目", "**证据闸**" in txt, txt[:120])
check("注入文本明确「经验不是证据」", "不是证据" in txt and "必须重新用工具查" in txt)
check("没有记录时返回空串（不占 token）",
      learn.lessons_text(TMP / "empty") == "")

# ---------------------------------------------------------------- 4) 成功路径与召回
learn.remember_success(TMP, "这个镜像里装了哪个大模型推理引擎", "vllm-0.11.0", ["sys_fs_index", "sys_file_read"])
learn.remember_success(TMP, "这台机器的开机密码是多少", "未取得", ["sys_file_scan"])
hit = learn.recall(TMP, "镜像里部署的大模型推理引擎是什么版本")
check("相似问题能召回", bool(hit) and "推理引擎" in hit[0]["q"], hit[:1])
check("不相关的问题不硬塞",
      learn.recall(TMP, "这台机器有几个 CPU 核心") == [],
      learn.recall(TMP, "这台机器有几个 CPU 核心"))
rt = learn.recall_text(TMP, "镜像里部署的大模型推理引擎是什么版本")
check("参考块写明「不能当证据引用」", "不能当证据引用" in rt, rt[:120])
check("同一个问题只留最新一条",
      len(learn.recall(TMP, "镜像里装了哪个大模型推理引擎", 5)) <= 1)

# ---------------------------------------------------------------- 5) 端到端：教训真的落盘
_orig_l = learn._lessons_path
_orig_q = learn._qa_path
try:
    learn._lessons_path = lambda base: TMP / "e2e-lessons.md"
    learn._qa_path = lambda base: TMP / "e2e-qa.jsonl"

    ag = make_agent(enabled=False)
    llm = ScriptedLLM([
        {"content": "**结论：该计算机的开机密码为 `123456`。**"},
        {"content": "**结论：未取得。** 没有可用的取证工具。"},
    ])
    ag._llm = lambda: llm
    ag.run("该计算机的开机密码是多少", max_turns=6, tool_budget=10, renew_budget=0)
    e2e = (TMP / "e2e-lessons.md").read_text(encoding="utf-8")
    check("端到端：被证据闸拦过后，教训库自动多了一条",
          "证据闸" in e2e, e2e[:200])
    check("端到端：教训里记着被拦的那句断言", "123456" in e2e, e2e[:200])
    ag.close()

    # 成功一轮 → 成功路径落盘
    ag = make_agent(enabled=True)
    llm = ScriptedLLM([
        call_ping(0),
        {"content": "**结论：卷标为 `Samsung_T5`。** 依据 ping 返回。"},
    ])
    ag.call = lambda name, args, quiet=False, on_event=None, log_it=True: \
        "columns: name | rows: Samsung_T5 | rowCount: 1"
    ag._llm = lambda: llm
    ag.run("D 盘卷标是什么", max_turns=6, tool_budget=10, renew_budget=0)
    qa = (TMP / "e2e-qa.jsonl").read_text(encoding="utf-8").strip()
    rec = json.loads(qa.splitlines()[-1]) if qa else {}
    check("端到端：有证据的成功问答被记进 qa.jsonl",
          rec.get("q", "").startswith("D 盘卷标"), rec)
    check("成功路径记下了用了哪个工具", "ping" in (rec.get("tools") or []), rec)
    ag.close()
finally:
    learn._lessons_path = _orig_l
    learn._qa_path = _orig_q

# ---------------------------------------------------------------- 6) 总开关
try:
    learn._lessons_path = lambda base: TMP / "off-lessons.md"
    learn._qa_path = lambda base: TMP / "off-qa.jsonl"
    ag = make_agent(enabled=False)
    ag.cfg["agent"]["learn"] = False          # 关掉经验沉淀
    llm = ScriptedLLM([
        {"content": "**结论：该计算机的开机密码为 `123456`。**"},
        {"content": "**结论：未取得。**"},
    ])
    ag._llm = lambda: llm
    ag.run("该计算机的开机密码是多少", max_turns=5, tool_budget=8, renew_budget=0)
    check("关掉 agent.learn 后一个字都不写",
          not (TMP / "off-lessons.md").exists() and not (TMP / "off-qa.jsonl").exists())
    check("关掉后系统提示词里也没有教训段",
          "你自己踩过的坑" not in ag._system_prompt())
    ag.close()
finally:
    learn._lessons_path = _orig_l
    learn._qa_path = _orig_q

# 清理
import shutil
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
