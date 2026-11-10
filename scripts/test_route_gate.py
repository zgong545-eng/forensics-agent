"""验证「撞墙 ≠ 查不到」：换路门禁 + 大文件流式扫描。

现场事故（2026-10-30 15:0x，用户亲测）：
  题目「WSL 中部署的大模型推理引擎名称和版本是什么？（答案格式：名称-版本，如llama.cpp-0.1.0）」
  模型的答复条理清楚、每条都附工具原文，最后还列了「下一步建议」——但结论是「未取得」。
  它撞到的三个墙其实都有现成通道：
    ① ges grep 报 file_too_large（ext4.vhdx 18.5 GB > 单文件 32 MiB）
    ② 索引 not_created，index build 报 HTTPError
    ③ 案件后台 8851 读超时（case_backend_unreachable）
  它一条都没换：没重连、没用裸数据检索、没把 vhdx 当检材挂载，
  只把「换路」写进了「下一步建议」——**这等于把证据弄丢了**，而且看起来完全合规。

本文件锁住两件事：
  A. 撞到阻塞型报错就写「未取得」、换路清单一条没走 → 打回，并把该走的通道列出来
  B. 真走了通道（重连 + 裸数据检索/本地扫描/挂载）→ 允许写「未取得」
  C. sys_file_scan 能扫超大文件（分块、双编码、字节偏移正确）

用法：python scripts/test_route_gate.py
"""
import os
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

from agent import (ForensicsAgent, Session, probe_magic,  # noqa: E402
                   routes_from_call, scan_file_stream)

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
        self.seen = []
        self.tools = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        self.tools.append([(t.get("function") or {}).get("name") for t in (tools or [])])
        if self.script:
            return self.script.pop(0)
        return {"content": "（兜底）"}


def make_agent(enabled=True):
    cfg = {
        "mcpServers": {"fake": {"command": PY, "args": [FAKE],
                                "enabled": enabled, "timeout": 20}},
        "skills": {"roots": [str(ROOT / "skills")], "allow_commands": ["ges"]},
        "agent": {}, "tool_timeout": 20,
    }
    return ForensicsAgent(cfg, Session(enabled=False), verbose=False,
                          cfg_path=ROOT / "config.json")


def call(name, args="{}", i=0):
    return {"content": "", "tool_calls": [{
        "id": f"c{i}", "type": "function",
        "function": {"name": name, "arguments": args}}]}


# ============================================================ A. 大文件流式扫描
tmpdir = Path(tempfile.mkdtemp(prefix="scan_"))
big = tmpdir / "ext4.vhdx"
SIZE = 40 * 1024 * 1024            # 40 MB，跨 5 个 8MB 块，专门测跨块命中
with big.open("wb") as f:
    f.write(b"vhdxfile" + b"\x00" * 1024)          # 魔数
    f.seek(3_000_000)
    f.write(b"....OLLAMA_VERSION=0.5.7....")       # ASCII
    f.seek(20_000_000)
    f.write(" llama.cpp-0.3.0 ".encode("utf-16-le"))  # 宽字符
    f.seek(SIZE - 1)
    f.write(b"\x00")

rep = scan_file_stream(big, ["ollama", "llama.cpp", "vllm"])
check("VHDX 魔数被识别成类型线索", "VHDX" in rep, rep.splitlines()[2])
check("ASCII 命中被找到", "ASCII/UTF-8" in rep and "OLLAMA_VERSION" in rep, rep[:600])
check("UTF-16LE 命中被找到（只按 ASCII 找会假阴性）",
      "UTF-16LE" in rep and "llama.cpp-0.3.0" in rep, rep[:900])
check("报告字节偏移，能回原文件定位",
      "3,000,0" in rep and "20,000,0" in rep, rep[:900])

rep0 = scan_file_stream(big, ["vllm"])
check("真没找到时明确说「不是没法扫」", "命中：0" in rep0 and "全量读过" in rep0, rep0[:400])

rep_cut = scan_file_stream(big, ["ollama"], max_bytes=1_000_000)
check("max_bytes 只扫前段时，命中数为 0 且明确「后面还没扫」",
      "命中：0" in rep_cut and "字节没扫" in rep_cut, rep_cut[:400])

# 分段续扫：一次只给 0.05 秒，必须能靠 start 续上，最后一并找到两处命中
seg1 = scan_file_stream(big, ["ollama", "llama.cpp"], max_seconds=0.02, chunk_mb=1)
check("单次时长上限会把结果切成段并给出续扫起点",
      "start=" in seg1 or "全量完成" in seg1, seg1[-300:])
import re as _re
m = _re.search(r"start=(\d+)", seg1)
offset = int(m.group(1)) if m else 0
total = ""
while m:
    nxt = scan_file_stream(big, ["ollama", "llama.cpp"], start=offset,
                           max_seconds=0.02, chunk_mb=1)
    total += nxt
    m = _re.search(r"start=(\d+)", nxt)
    if m:
        offset = int(m.group(1))
check("续扫能把整份文件读完（分段 ≠ 抽样，最后一并能命中）",
      "llama.cpp=1" in seg1 + total, (seg1 + total)[-400:])

check("probe_magic 认得出 ELF/PE", "ELF" in probe_magic(b"\x7fELF\x02\x01")
      and "PE" in probe_magic(b"MZ\x90\x00"))

check("routes_from_call：sys_file_scan 记成「本地扫描」",
      routes_from_call("sys_file_scan", {"path": "x"}) == {"local"})
check("routes_from_call：ges copy 记成「复制到本地」",
      "copy" in routes_from_call("sys_skill_exec", {"command": "ges copy /x /y"}))
check("routes_from_call：ges case open 记成「重连」",
      "reconnect" in routes_from_call("sys_skill_exec", {"command": "ges case open 20261012143610X"}))

# ============================================================ B. 换路门禁
BLOCKER_OUT = ('{"code":"file_too_large","operation":"grep",'
               '"message":"Evidence file /evidence_mounts/eid-1/ext4.vhdx exceeds 33554432 bytes."}')
GIVEUP = ("**结论：未取得。** 本轮无法确认 WSL 中部署的大模型推理引擎名称与版本。\n\n"
          "| 项目 | 值 | 出处 |\n|---|---|---|\n"
          "| 该 vhdx 无法直接检索 | `file_too_large` | ges grep 返回 |\n\n"
          "**下一步建议**：恢复 GES 案件服务后，把 ext4.vhdx 作为独立检材导入再检索。")


def stub(ag, blocker=True):
    orig = ag.call

    def fake_call(name, args, quiet=False, on_event=None, log_it=True):
        if name == "ges_grep":
            return BLOCKER_OUT
        if name == "raw_data_content_search":
            return '{"matches":[],"total":0,"source_complete":true}'
        return orig(name, args, quiet=quiet, on_event=on_event, log_it=log_it)

    ag.call = fake_call


# B1：撞墙 + 未取得 + 一条换路都没走 → 必须打回
ag = make_agent()
stub(ag)
llm = ScriptedLLM([
    call("ges_grep", '{"pattern":"ollama"}', 0),
    {"content": GIVEUP},
    {"content": GIVEUP},
    {"content": GIVEUP},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("WSL中部署的大模型推理引擎名称和版本是什么？（答案格式：名称-版本，英文全小写，如llama.cpp-0.1.0）",
                 max_turns=8, tool_budget=20, renew_budget=0, on_event=events.append)
gates = [e for e in events if e.get("type") == "gate"]
check("撞到 file_too_large 就交白卷 → 被换路门禁拦下", len(gates) >= 1, gates)
check("门禁最多打回 2 次（不许把用户永久卡住）", len(gates) == 2, len(gates))
gate_msg = ""
for snap in llm.seen:
    for m in snap:
        if "换路门禁" in str(m.get("content") or ""):
            gate_msg = str(m.get("content"))
check("打回时点明「这条通道走不通 ≠ 东西不在」",
      "只说明" in gate_msg and "通道" in gate_msg, gate_msg[:300])
check("打回时给出裸数据检索这条通道",
      "raw_data_content_search" in gate_msg, gate_msg[:400])
check("打回时给出复制到本地 + sys_file_scan 这条通道",
      "sys_file_scan" in gate_msg and "ges copy" in gate_msg, gate_msg[:600])
check("打回时给出当独立检材挂载这条通道",
      "add_image_material" in gate_msg and "load_case" in gate_msg, gate_msg[:800])
check("打回时点名「重连/重开案件」",
      "sys_server_reconnect" in gate_msg and "case open" in gate_msg, gate_msg[:900])
check("打回时点出它撞的具体报错（可核对）",
      "file_too_large" in gate_msg, gate_msg[:300])
check("打回两次后仍未换路 → 放行，不卡死，最终答复仍是未取得（不编值）",
      "未取得" in text and "llama.cpp-0.1.0" not in text, text[:160])
ag.close()

# B2：先重连、再换通道，然后写未取得 → 放行
ag = make_agent()
stub(ag)
llm = ScriptedLLM([
    call("ges_grep", '{"pattern":"ollama"}', 0),
    {"content": "", "tool_calls": [
        {"id": "r1", "type": "function",
         "function": {"name": "sys_server_reconnect", "arguments": "{}"}},
        {"id": "r2", "type": "function",
         "function": {"name": "raw_data_content_search", "arguments": '{"keyword":"ollama"}'}},
    ]},
    {"content": "**结论：未取得。** 已重连并跑裸数据检索，ollama/llama.cpp 均 0 命中；"
                "下一步只能人工确认案件服务。"},
])
ag._llm = lambda: llm
events = []
text, _ = ag.run("WSL中部署的大模型推理引擎名称和版本是什么？", max_turns=8,
                 tool_budget=20, renew_budget=0, on_event=events.append)
check("走了重连 + 裸数据检索后，允许写未取得（不误拦）",
      not any(e.get("type") == "gate" for e in events) and "未取得" in text,
      f"gate={[e.get('text') for e in events if e.get('type') == 'gate']} / {text[:100]}")
ag.close()

# B3：没有撞墙，正常答「查无此项」→ 不该被拦
ag = make_agent()
llm = ScriptedLLM([{"content": "**结论：未取得。** 全盘检索 ollama 无命中，属于确实没有部署。"}])
ag._llm = lambda: llm
events = []
text, _ = ag.run("WSL 里装了大模型吗？", max_turns=4, tool_budget=10,
                 renew_budget=0, on_event=events.append)
check("没撞墙时「查无此项」不被误拦",
      not any(e.get("type") == "gate" for e in events), [e.get("type") for e in events])
ag.close()

# B4：确实是人工阻塞（软件没装）→ 允许直接说，不再逼它换路
ag = make_agent()
stub(ag)


def fake_soft(name, args, quiet=False, on_event=None, log_it=True):
    if name == "ges_grep":
        return "【命令失败 rc=1】ERROR: 超级取证大师软件未安装，无法执行检索"
    return ""


ag.call = fake_soft
llm = ScriptedLLM([call("ges_grep", "{}", 0),
                   {"content": "**结论：未取得。** 这台机器上超级取证大师软件未安装，"
                               "属人工阻塞，需要先装软件。"}])
ag._llm = lambda: llm
events = []
text, _ = ag.run("WSL 里装了大模型吗？", max_turns=6, tool_budget=10,
                 renew_budget=0, on_event=events.append)
check("人工阻塞（软件未安装）时允许直接写未取得，不反复打回",
      not any(e.get("type") == "gate" for e in events), [e.get("type") for e in events])
ag.close()

# ============================================================ C. 技能名 = CLI 名
ag = make_agent()
sk = ag.skills.get("ges")
check("sys_skill_exec name=\"ges\" 能解析到 ges-case-analysis（CLI 名反查）",
      bool(sk) and "ges" in (sk.name or ""), getattr(sk, "name", None))
ag.close()

for f in [big]:
    try:
        f.unlink()
    except Exception:
        pass
try:
    os.rmdir(tmpdir)
except Exception:
    pass

print(f"\n合计 {sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
