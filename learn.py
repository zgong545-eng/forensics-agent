# -*- coding: utf-8 -*-
"""经验沉淀 —— 让这个取证 agent 越用越熟。

它不会改模型权重，也不会自己改安全规则（那三条闸门是硬边界，被"学习"改掉就是灾难）。
这里做的是两件**可控、可回退**的事：

1. **教训库** `prompts/lessons.md`
   本轮出现「被证据闸打回 / 交白卷被打回 / 自称没工具被抓 / 工具熔断 / 用户插话纠正 / 撞墙」
   时，自动追加一条结构化记录；下次拼系统提示词时带最近 N 条。
   —— 让它在同一个坑上不栽第二次。

2. **成功路径** `logs/memory/qa.jsonl`
   本轮正常交付（有工具证据、没认输）时，记下「问题 → 答案首段 → 用了哪些工具」。
   下次遇到相似问题，先给它看一眼前人怎么解的。
   —— 省掉重复摸索。

两条铁律（写在注入文本里，也写在这里提醒自己）：
- **经验不是证据**。参考块只给思路，任何具体值都必须重新用工具查。
- **只增不改**。不覆盖 prompts/system.md、不碰 agent.py 里的判定规则，出问题直接删文件即可回退。

纯标准库，不新增依赖。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

LESSON_MAX = 60          # lessons.md 最多留多少条（超出砍最旧的）
LESSON_INJECT = 12       # 每次拼提示词带几条
QA_MAX = 400             # 成功路径最多留多少条
QA_INJECT = 2            # 最多参考几条
SIM_MIN = 0.22           # 相似度门槛，低于这个宁可不给


# ---------------------------------------------------------------- 工具
def _short(s: str, n: int = 46) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _grams(s: str) -> set[str]:
    """中文没有空格，用字符二元组算重合度；英文顺便也会被切碎，不影响排序。"""
    s = re.sub(r"[\s\W_]+", "", (s or "").lower())[:200]
    if not s:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _sim(a: str, b: str) -> float:
    A, B = _grams(a), _grams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _root(base: Path) -> Path:
    """落盘根目录，带两道改道，防止测试污染真实经验库。

    ① `LEARN_DIR` 环境变量（测试脚本显式指定）；
    ② 兜底：脚本名以 `test_` 开头时自动落到 `logs/_test/`。
       没这条的话，跑一遍用例就会把「D 盘卷标是什么」「测试中途中断」这种
       假问答写进真实的 lessons.md / qa.jsonl，用户点开看见一堆鬼东西。
    """
    env = (os.environ.get("LEARN_DIR") or "").strip()
    if env:
        return Path(env)
    if os.path.basename(sys.argv[0] or "").startswith("test_"):
        return base / "logs" / "_test"
    return base


def _lessons_path(base: Path) -> Path:
    return _root(base) / "prompts" / "lessons.md"


def _qa_path(base: Path) -> Path:
    return _root(base) / "logs" / "memory" / "qa.jsonl"


# ---------------------------------------------------------------- 1) 教训库
def classify(stats: dict) -> tuple[str, str]:
    """把一轮的"翻车信号"归成一个类别 + 一句人话。

    优先级是有讲究的：用户亲口纠正 > 闸门打回 > 工具熔断 > 撞墙。
    前面的是"人已经生气了"，后面的是"自己绕了很久"，前者更该被记住。
    """
    corrects = [c for c in (stats.get("corrects") or []) if (c or "").strip()]
    if corrects:
        return "用户纠正", f"用户插话「{_short(corrects[-1], 60)}」"

    gates = int(stats.get("gates") or 0)
    if gates:
        return "证据闸", f"被拦 {gates} 次：{_short(stats.get('gate_hint', ''), 60)}"

    if int(stats.get("surrender") or 0):
        return "白卷闸", f"撞的是「{_short(stats.get('blocker', ''), 40)}」，却直接写未取得"

    if int(stats.get("selfcheck") or 0):
        return "自称没工具", "手上有工具却说没有，被强制核实"

    fused = stats.get("fused") or []
    if fused:
        return "工具熔断", f"{'、'.join(fused[:3])} 连续失败，本任务内被摘掉"

    blockers = stats.get("blockers") or []
    if blockers:
        tool, word = blockers[-1]
        return "撞墙", f"{tool} 报「{_short(word, 40)}」"

    return "", ""


def record_round(base: Path, prompt: str, answer: str, stats: dict) -> str:
    """把这一轮的教训写进 lessons.md，返回写进去的那一行（没写就返回空串）。"""
    kind, detail = classify(stats)
    if not kind:
        return ""

    line = f"- `{_now()}` **{kind}**｜{detail}｜题：{_short(prompt)}"
    p = _lessons_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    # 去重：同一类 + 同样的细节前 24 字，只留最新那条（时间戳会变，所以按"内容指纹"比）
    fp = (kind + detail)[:24]
    kept = [l for l in old
            if not (l.startswith("- ") and fp in l)]
    kept.append(line)

    # 只保留最近 LESSON_MAX 条，头部写个说明免得用户不知道这文件是干嘛的
    body = kept[-LESSON_MAX:]
    head = [
        "# 教训库（agent 自己攒的，可以随手删）",
        "",
        "> 每轮出现「被闸门打回 / 交白卷 / 自称没工具 / 工具熔断 / 用户纠正 / 撞墙」时自动追加一条，",
        "> 下次拼系统提示词会带上最近若干条。**删掉某行就等于让它忘掉这条教训。**",
        "",
    ]
    p.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    return line


def lessons_text(base: Path, limit: int = LESSON_INJECT) -> str:
    """拼进系统提示词的那一段。没有记录就返回空串（不占 token）。"""
    p = _lessons_path(base)
    if not p.exists():
        return ""
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
    if not lines:
        return ""
    return ("\n## 你自己踩过的坑（本项目实测积累，逐条都真发生过）\n"
            + "\n".join(lines[-limit:])
            + "\n\n这些是**经验提示**，不是证据：它告诉你「上次哪条路走不通」，"
              "不告诉你「这次的答案是什么」。任何具体值都必须重新用工具查，"
              "不许拿这里的内容当结论，也不许因为这里没提过就认为无路可走。\n")


# ---------------------------------------------------------------- 2) 成功路径
def remember_success(base: Path, prompt: str, answer: str,
                     tools: list[str] | None = None) -> None:
    """记一条"这题当时是怎么解的"。只存思路，不存证据原文（文件会越来越大）。"""
    p = _qa_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "t": _now(),
        "q": _short(prompt, 120),
        "a": _short(answer, 260),
        "tools": [t for t in (tools or []) if t][:8],
    }
    old = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                old.append(json.loads(line))
            except Exception:
                continue
    # 同一个问题只留最新一次，别让重复提问把库撑爆
    old = [o for o in old if _sim(o.get("q", ""), prompt) < 0.92]
    old.append(rec)
    old = old[-QA_MAX:]
    p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in old) + "\n",
                 encoding="utf-8")


def recall(base: Path, prompt: str, top: int = QA_INJECT) -> list[dict]:
    """找几条历史上问过、且当时真解出来的同类问题。"""
    p = _qa_path(base)
    if not p.exists():
        return []
    hits = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        s = _sim(rec.get("q", ""), prompt)
        if s >= SIM_MIN:
            hits.append((s, rec))
    hits.sort(key=lambda x: -x[0])
    return [r for _, r in hits[:top]]


def recall_text(base: Path, prompt: str, top: int = QA_INJECT) -> str:
    """把命中项拼成一段"参考"，塞在用户问题后面。"""
    hits = recall(base, prompt, top)
    if not hits:
        return ""
    parts = ["\n\n---\n**参考：以前问过类似的问题，当时的解法（只供思路，不是本次证据）**"]
    for i, h in enumerate(hits, 1):
        tools = "、".join(h.get("tools") or []) or "（没记录）"
        parts.append(f"{i}. 问题：{h.get('q', '')}\n   当时的结论：{h.get('a', '')}\n"
                     f"   用到的工具：{tools}")
    parts.append("**这些内容不能当证据引用**。做法可以照抄，结论必须重新查一遍。")
    return "\n".join(parts) + "\n"
