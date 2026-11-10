#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
电子数据取证智能体（MYOS 版） — agent.py

一个零第三方依赖的 AI Agent：
  · 手写 MCP stdio 客户端，直连 myos_mcp_server.exe（49 个取证工具）
  · 手写 OpenAI 兼容 chat/completions 适配层（DeepSeek / 通义 / Kimi / 本地 vLLM 均可）
  · 自动 ReAct 循环：自然语言 → 模型选工具 → 执行 → 回填结果 → 出结论
  · 破坏性操作强制人工确认
  · 无模型也能用：--shell / --call 直接敲工具

用法见 README.md，或 python agent.py -h
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from skills import SkillRegistry

import learn                       # 经验沉淀：教训库 + 成功路径

# --------------------------------------------------------------------------
# Windows 控制台 UTF-8 修正（否则中文乱码 / UnicodeEncodeError）
# --------------------------------------------------------------------------
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
REPORT_DIR = BASE_DIR / "reports"

# 名字像「查状态」的工具：轮询是它们的正常用法，不该被「同参数重复」判定拦下
POLL_HINTS = re.compile(r"wait|status|progress|poll|completion|\bjob\b|\btask\b", re.I)

# 「手上明明有工具却说没有」——这是模型最坑的一种自信错误：一次工具都没调，
# 就断言「服务器被禁用 / 可用工具数为 0」，然后让用户去改配置文件。
# 命中就强制它核实一次，不能把这种答复当结论交出去。
NO_TOOL_CLAIM = re.compile(
    r"(没有(任何)?(可用)?工具|工具(全部)?不可(调|用)|可用工具数(为|是|:)?\s*0|"
    r"服务器(处于)?(禁用|未启用)|工具链(仍未|未|没有)|0\s*个可用工具)", re.I)

# ---- 「答案格式：如 XXX」里的 XXX 是格式示例，不是答案 ----
# 实测事故：题目写「（答案格式：以实际为准，如 Tuo_Jie）」，模型答 Tuo_Jie；
# 题目写「如 123456」，第三次追问时模型直接答 123456，还配了一张「字典命中」的证据表。
# 这不是它"猜"的，是它把示例当成了线索。**示例值必须从送模型的文本里抹掉**，
# 只保留格式说明，否则再强的提示词也压不住。
EXAMPLE_HINT = re.compile(r"(答案格式|格式|例如|示例|参考值|样例)")
EXAMPLE_VALUE = re.compile(r"(如|例如|示例[:：]?|参考[:：]?)\s*[`'\"“]?([A-Za-z0-9_\-]{2,24})[`'\"”]?")

# 「硬断言」：这类句子一旦出现具体值，就必须有工具返回撑腰，否则就是编造。
HARD_CLAIM = re.compile(
    r"(密码|口令|哈希|NTLM|LM\s*Hash|bootkey|字典命中|破解成功|已解密|明文|卷标|序列号|"
    r"IP\s*地址|MAC\s*地址|账号为|用户名为|序列号为)", re.I)
# 留有余地的说法——有它们就说明模型没把话说死，不算硬断言
HEDGE = re.compile(
    r"(未取得|无法|未能|不能确定|不确定|查不到|没有(直接)?证据|未确认|暂缺|推测|"
    r"待确认|无法验证|无法读取|不具备条件|支持不足|不能仅凭|未做|未经)", re.I)

# ---- 「说了『密码』二字」不等于「断言了一个密码」 ----
# 实测事故（2026-10-30 19:2x，用户亲测）：用户只是问「你能干什么」，
# 模型答了一份能力清单，其中有一句「需要密码（BitLocker、手机锁屏、加密文档）」——
# 这是在讲**能力边界**。结果被裸关键词 `密码` 判成「无证据硬断言」，打回要求重写，
# 模型于是交出「结论：未取得 / 关于「密码」一词 / 已确认·未确认 表格」这套自证清白的白卷。
# 用户看到的东西比原答案还蠢。根因是判定条件写成了「命中词表」，而正确的条件是
# **命中词表 + 真的给出了一个具体值**。没有具体值的话，根本没有"编造"的余地。
SPECIFIC_VALUE = re.compile(
    r"(`[^`\n]{2,80}`|「[^」\n]{1,60}」|\"[^\"\n]{2,60}\"|'[^'\n]{2,60}'|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,10}|"      # 邮箱
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|"                            # IP
    r"\b[0-9a-fA-F]{16,}\b|"                                   # 哈希 / GUID
    r"[A-Za-z]:\\\\|/mnt/[a-z]/|\\\\\.\\\\|"                    # 盘符/挂载路径
    r"\b\d{4,}\b)")                                            # 4 位以上数字（含 123456）
QUOTED = re.compile(r"(`[^`\n]{2,80}`|「[^」\n]{1,60}」|\"[^\"\n]{2,60}\"|'[^'\n]{2,60}')")
# 被引号包着、但其实是「路径 / 命令」的东西，不当成"值"：
# 能力说明里写「我能读 `C:/hlnet/...`」不该被判成"给出了一个具体结论"。
PATHISH = re.compile(r"^([A-Za-z]:[\\/]|/|\\\\|\.{1,2}/|https?://)")
_YEARISH = re.compile(r"^(19|20)\d{2}$")


def is_hard_claim(text: str) -> bool:
    """真正的硬断言 = 词表命中 **且** 给出了具体值。

    只命中词表（「需要密码」「卷标会显示」「IP 地址可以定位」）是能力/流程说明，
    不是结论，不能拿去当"无证据"打回——那样只会逼模型交白卷。
    """
    if not text or not HARD_CLAIM.search(text):
        return False
    for m in SPECIFIC_VALUE.finditer(text):
        raw = m.group(0)
        v = raw.strip("`「」\"'")
        if not v or _YEARISH.match(v):
            continue
        if QUOTED.fullmatch(raw) and (" " in v or "\\" in v or PATHISH.match(v)):
            continue        # 引号里的是命令或路径，不是"值"
        return True
    return False


# ---- 元问题：问的是「你」而不是「案子」 ----
# 这类提问不需要检材、不需要工具、更不需要证据链。此前把能力介绍也塞进
# 取证闸门，才会出现「问你能干什么 → 答未取得」这种荒唐结果。
META_ASK = re.compile(
    r"(你(?:能|会|可以|都|想|要){0,3}(?:干|做|帮|查|看|分析|搞定)[我你他]?(?:干|做)?(?:些|点|哪些)?"
    r"(?:什么|啥|哪些|嘛|毛)|"
    r"你(?:能|会|可以|都){1,3}(?:些|点)?(?:什么|啥|哪些)|"
    r"你(?:是|有|都)(?:什么|啥|哪些)(?:能力|功能|本事)|"
    r"(?:有|支持)(?:什么|哪些|啥)(?:功能|能力|工具|玩法|用法)|"
    r"(?:介绍|说说|讲下|讲讲|聊聊)(?:一下|下)?(?:你自己|你的能力|你能)|"
    r"你(?:是|是干)(?:什么|嘛)的|你是谁|"
    r"(?:怎么|如何)(?:用|使用|开始|上手)|使用说明|说明书|"
    r"(?:agent|智能体|系统|工具)\s*(?:都)?(?:能|可以)(?:干|做)(?:什么|啥|嘛)|"
    r"^(?:help|帮助|\?|？)$)", re.I)
# 带了具体标的问句 = 真任务，哪怕里面夹着「你能干什么」也不算元问题
# （实测反例：「帮我分析这个案子，你能干什么都行，先把浏览器记录拉出来」）
TASK_HINT = re.compile(
    r"(案件号|案号|检材|镜像|[A-Za-z]:[\\/]|\.E0\d|\.vhdx|\.dd\b|挂载|路径|"
    r"先把|然后|\.csv|\.db\b|浏览器(记录|历史)|聊天记录|这个案子|这个案件|这份)", re.I)


def is_meta_ask(prompt: str) -> bool:
    """用户这一问是不是在问「你能干什么 / 怎么用」。

    两道限制，避免误判：
      ① 只在**短问句**里认（长句多半是带着任务来的）；
      ② 句子里出现具体标的（案件号、路径、镜像名、文件名）就不算元问题。
    """
    p = (prompt or "").strip()
    if not p or len(p) > 120:
        return False
    if TASK_HINT.search(p):
        return False
    return bool(META_ASK.search(p))


# 一条答复里如果出现这些，说明它确实在讲「我能做什么」，不是白卷
CAPABILITY_RE = re.compile(
    r"(我能|我可以|我可以帮|能帮|支持|会读|能读|能查|能分析|能看|功能|技能|能力范围|"
    r"内置工具|挂载视图|案件号)", re.I)
# 「自证清白」形态：不打回的话模型会把整条答复写成自我辩护书
SELF_JUSTIFY = re.compile(
    r"(关于「[^」\n]{1,16}」一词|上一条(答复|回答)(中|里|只)|本条答复(不含|未出现|没有|里没有)|"
    r"能力边界说明|不是断言|已确认\s*[/·]\s*未确认|全文(没有|未)出现任何)", re.I)

def scrub_examples(text: str) -> str:
    """把「答案格式：如 XXX」里的具体示例值换成占位符，防止模型把示例当答案。

    只在**同一句里出现「格式/示例/例如」这类词**时才动手，避免误伤正文里的普通「如」。
    落盘和界面显示仍然用原文——用户看到的必须是他自己发的字。
    """
    if not text or not EXAMPLE_HINT.search(text):
        return text
    out = []
    for line in text.splitlines():
        # 一行里既有"格式/示例"提示，又有"如 xxx" → 这一行里的示例值不可信
        if EXAMPLE_HINT.search(line):
            line = EXAMPLE_VALUE.sub(lambda m: f"{m.group(1)}<示例·仅演示格式，勿当答案>", line)
        out.append(line)
    return "\n".join(out)

# 「看一眼」类工具：它们返回的是说明书/状态，不是案件数据。
# 计「本轮有几条证据」时必须排除——否则模型读了两份技能文档就以为自己有据可依，
# 然后凭空写出一条「ges read ... 返回 data.csv，登录密码 260919」的假证据。
INTROSPECTION_TOOLS = frozenset({
    "sys_server_status", "sys_server_reconnect", "sys_skill_read", "sys_skill_list",
})

def unsupported_values(answer: str, haystack: str) -> list[str]:
    """找出答案里「引号包着、却在本轮工具返回里找不到」的具体值。

    这是最硬的一道核对：模型很容易写出「`ges read ...` 返回了 data.csv，登录密码 `260919`」
    这种句子——工具其实一次都没跑成，值全是它编的。只靠「有没有成功调用」拦不住，
    必须回到原文里逐字找。

    只查带数字/下划线/点号的 token（`260919`、`Samsung_T5`、`103.87.9.67`），
    纯英文词（`full address`、`data.csv` 之类）不查——那些多在技能文档和正文里出现，误杀率高。
    """
    if not answer or not haystack:
        return []
    pool = re.sub(r"[\s`'\"“”「」]", "", haystack).lower()
    bad: list[str] = []
    for m in re.finditer(r"[`「\"“]([A-Za-z0-9_\-.:@\\/]{4,60})[`」\"”]", answer):
        val = m.group(1)
        if not re.search(r"[0-9]", val):     # 不含数字的多半是名字/命令，跳过
            continue
        if re.sub(r"[\s`'\"“”「」]", "", val).lower() not in pool:
            bad.append(val)
    return bad


# ---- 「撞墙」不是「查不到」----
# 现场事故（2026-10-30 14:5x，用户亲测）：问「WSL 里部署的大模型推理引擎名称与版本」，
# 模型的表现堪称模范——条理清楚、每条都附工具原文、最后还写了「下一步建议」。
# 但结论是「未取得」，而它撞到的三个墙全都有现成通道：
#   ① ges grep 报 file_too_large（ext4.vhdx 18.5 GB > 单文件 32 MiB）
#   ② 案件索引 not_created，index build 报 HTTPError
#   ③ 案件后台 8851 读超时（case_backend_unreachable）
# 它一条都没换路：没 re connect、没用 raw_data_content_search（裸数据检索不受单文件上限限制）、
# 没把 vhdx 当独立检材挂载，就把「换路」写进了「下一步建议」。
# 取证里最贵的错不是编答案，是**把本来能查的写成查不到**——这等于把证据弄丢了。
BLOCKER_RE = re.compile(
    r"(file_too_large|exceeds\s+\d+\s+bytes|exceeds\s+the\s+\d+|文件(过|太)大|"
    r"超过.{0,8}(上限|限制|字节)|HTTPError|case_backend_unreachable|"
    r"Read\s+timed\s+out|读超时|连接被拒|Connection\s+(refused|aborted)|"
    r"not_created|索引(未建立|未创建|未就绪|不可用)|index_state|coverage_hint|"
    r"Index\s+not\s+found|索引不存在|未建立(文件)?索引|"
    r"无法直接检索|不支持.{0,8}(keyword|检索|grep)|文件数.{0,6}超过)", re.I)

# 「交白卷」的措辞。注意：这些词本身没错——**查不到就该说查不到**。
# 只有跟 BLOCKER 同时出现、且换路清单一条都没试过时，才判为「过早收工」。
GIVEUP_RE = re.compile(
    r"(未取得|无法(确认|确定|回答|得出|获取)|查不到|未能|不能确定|未确认|无(法)?(直接)?检索)", re.I)

# 真正需要人来解决的阻塞——这种写「未取得」是正确做法，不该被门禁反复打回。
HUMAN_BLOCK_RE = re.compile(
    r"(软件(没|未)(启动|安装|打开)|未安装|尚未安装|需要授权|需人工|需要密码|请输入密码|"
    r"未购买|licen[cs]e|找不到.{0,6}(exe|程序)|没有安装)", re.I)

# 「把决定权推回给用户」——实测模型最爱这么收尾：
#   「需要我现在就按第 2 条去挂载视图里逐个定位吗？」「要我走这条路吗？」
# 用户已经明说「你去查，我只要答案」，还在问，就是拿提问当免责。
# 判据只看**结尾**：中间的解释性设问是正常的，收口处征求许可才是推责。
ASK_PERMISSION = re.compile(
    r"(要不要|是否需要|是否要|需要我|要我|希望我)[^\n]{0,60}?(吗|呢)?\s*[?？]\s*$|"
    r"(要我|需要我)[^\n]{0,60}$", re.I)

# 换路的符号化编号：门禁要能判断「它到底试过几条路」，而不是只听它自己说。
ROUTE_LABEL = {
    "reconnect": "重连服务器 / 重开案件",
    "raw": "裸数据检索（raw_data_content_search）",
    "copy": "把大文件复制到本地后再扫",
    "local": "本地流式扫描（sys_file_scan）",
    "mount": "把镜像/虚拟磁盘当独立检材挂载",
    "index": "重建文本索引",
}
# 工具名 → 路线编号。sys_skill_exec 要看命令内容，单独判。
ROUTE_BY_TOOL = {
    "sys_server_reconnect": "reconnect",
    "raw_data_content_search": "raw",
    "global_search_content": "raw",
    "quick_search_content": "raw",
    "quick_search_files": "raw",
    "add_image_material": "mount",
    "submit_computer_forensic_task": "mount",
    "create_file_index_task": "index",
    "sys_fs_index": "index",
    "sys_file_scan": "local",
    "sys_file_read": "local",
}
ROUTE_BY_CMD = (
    ("case open", "reconnect"),
    ("case status", "reconnect"),
    (" status", "reconnect"),
    ("copy ", "copy"),
    ("index build", "index"),
    ("index create", "index"),
    ("add material", "mount"),
    ("image add", "mount"),
)


def routes_from_call(name: str, args: dict | None) -> set[str]:
    """一次工具调用算走了哪几条换路通道。"""
    out: set[str] = set()
    route = ROUTE_BY_TOOL.get(name)
    if route:
        out.add(route)
    if name == "sys_skill_exec":
        cmd = " " + str((args or {}).get("command") or "").lower()
        for needle, rid in ROUTE_BY_CMD:
            if needle in cmd:
                out.add(rid)
    return out


# ---- 文件系统索引：一次 SQL 定位路径（比裸数据检索快两个数量级） ----
# 检材的 data/ 目录下有两份东西，用法完全不同：
#   FSData.mfs      主库。TBL_FS_ENTRY_<GUID> 里有 filename / filepath / filesize / isfile，
#                   但文件头是加密的，直连 sqlite3 报 "file is not a database"，
#                   只有厂商的 sqlite_query 能开（它自带解密）。
#   FSDataIndex.mfs 明文 SQLite，只有 FTS 组件（*_virtual_content 里 c0=文件名、c1=nodeId）。
#                   能离线确认「这个文件在不在」，但拿不到全路径。
# 所以查询走两条腿：优先 MCP sqlite_query 拿完整路径；它不可用时退回明文索引，
# 至少给出「文件确实存在 + nodeId」，别让模型把「打不开库」当成「没有这个文件」。
CASE_ROOTS = (
    "D:/MYOS/Case",
    "D:/Electronic Date Forensics/MYOS/Case",
    "E:/Electronic Date Forensics/MYOS/Case",
    "C:/Electronic Date Forensics/MYOS/Case",
    "F:/Electronic Date Forensics/MYOS/Case",
)


def find_case_dbs(explicit: str = "") -> list[Path]:
    """定位案件的 FSData.mfs（多个案件时按修改时间倒序，新的在前）。"""
    cands: list[Path] = []
    raw = (explicit or "").strip().strip('"').strip("'")
    if raw:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_file():
            cands.append(p)
        elif p.is_dir():
            for pat in ("FSData.mfs", "data/FSData.mfs", "*/data/FSData.mfs",
                        "*/dev/*/data/FSData.mfs"):
                cands.extend(sorted(p.glob(pat)))
    if not cands:
        for root in CASE_ROOTS:
            base = Path(root)
            if base.is_dir():
                cands.extend(base.glob("*/dev/*/data/FSData.mfs"))
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        k = str(c).lower()
        if k in seen or not c.is_file():
            continue
        seen.add(k)
        out.append(c)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def pick_entry_table(names: list[str]) -> str | None:
    """从 sqlite_master 的结果里挑出**真正的文件条目表**。

    库里同时存在 `TBL_FS_ENTRY_SOURCE`（源映射，没有文件条目）和
    `TBL_FS_ENTRY_<32位GUID>`（条目表）。按出现顺序取第一张会正好取到 SOURCE，
    然后查询返回 0 行 —— 看起来像「这台机器没有这个文件」，其实查错了表。
    """
    clean = [str(n) for n in (names or []) if str(n).strip()]
    guid = [n for n in clean if re.fullmatch(r"TBL_FS_ENTRY_[0-9A-Fa-f]{32}", n)]
    if guid:
        return guid[0]
    rest = [n for n in clean if not n.upper().endswith("_SOURCE")]
    return rest[0] if rest else None


def fs_entry_tables(conn: sqlite3.Connection) -> list[str]:
    """主库里的文件条目表（排除 FTS 的 _virtual_* 附属表）。"""
    rows = conn.execute(
        "select name from sqlite_master where type='table' "
        "and name like 'TBL_FS_ENTRY%' and name not like '%\\_virtual%' escape '\\'"
    ).fetchall()
    return [r[0] for r in rows]


def open_plain_db(path: Path) -> sqlite3.Connection | None:
    """只读打开明文 SQLite；不是明文库（客户库被加密）就返回 None。"""
    try:
        conn = sqlite3.connect("file:" + str(path).replace(os.sep, "/") + "?mode=ro&immutable=1",
                               uri=True, timeout=8)
        conn.execute("select count(*) from sqlite_master").fetchone()
        return conn
    except Exception:
        return None


def plain_index_search(db_index: Path, keywords: list[str],
                       limit: int = 40) -> list[tuple[str, int]]:
    """明文 FSDataIndex.mfs 里按文件名搜（FTS 组件表 c0=文件名、c1=nodeId）。

    457 MB 的表里 like 扫一遍约 0.5 秒 —— 比裸数据检索跑二十分钟划算得多。
    """
    conn = open_plain_db(db_index)
    if conn is None:
        return []
    try:
        tabs = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' "
            "and name like '%_virtual_content'").fetchall()]
        if not tabs:
            return []
        # 多个检材时会有多组，优先挑带 32 位 GUID 的那组
        guid_tabs = [t for t in tabs if re.search(r"[0-9A-Fa-f]{32}", str(t))]
        t = (guid_tabs or tabs)[0]
        hits: list[tuple[str, int]] = []
        seen: set[str] = set()
        for kw in keywords:
            sql = f"select c0, c1 from '{t}' where lower(c0) like ? limit ?"
            for name, nid in conn.execute(sql, (f"%{kw.lower()}%", limit)):
                key = f"{name}|{nid}"
                if key in seen:
                    continue
                seen.add(key)
                hits.append((str(name), int(nid or 0)))
                if len(hits) >= limit:
                    return hits
        return hits
    except Exception:
        return []
    finally:
        conn.close()


# ---- 读文本文件：镜像里的配置五花八门，编码猜错就全是乱码 ----
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030", "utf-16", "latin-1")


def osmap_roots() -> list[Path]:
    """本机已被取证软件挂载出来的镜像根目录（`C:/osmap/<GUID>_<时间>/Root`）。

    注意：Dokan 挂载点对 `stat()` 会偶发 `WinError 1450（系统资源不足）`，
    所以这里**不做 is_dir / stat 判断**——只拼候选，能不能读由后面 exists() 说话。
    用 glob('*/Root') 会在内部 stat 时直接抛异常，整个函数返回空列表，
    结果就是「明明挂载着，工具却说找不到文件」。
    """
    out: list[Path] = []
    for base in ("C:/osmap", "D:/osmap", "E:/osmap", "F:/osmap"):
        try:
            with os.scandir(base) as it:
                for e in it:
                    if e.name.lower().startswith(("process_mutex", "process_")):
                        continue
                    # 只拼候选，不调 is_dir / stat：Dokan 挂载点上的这两类调用
                    # 会偶发 WinError 1450 甚至直接卡住，能不能读让 exists() 去说。
                    out.append(Path(e.path) / "Root")
        except OSError:
            continue
    # 目录名自带时间戳（<GUID>_20261030101134），按名字倒序就是「新的在前」，零 IO
    out.sort(key=lambda p: p.parent.name, reverse=True)
    return out


def resolve_image_path(raw: str, mount: str = "") -> Path | None:
    """把「镜像内路径」变成「本地真实路径」。

    模型从 sys_fs_index 拿到的是 `/Partition0_系统/Users/pc/...` 这种镜像内路径，
    而实际文件在 `C:/osmap/<GUID>/Root/Partition0_系统/...`。让它自己拼极易拼错
    （少一层 Root、盘符写成 C:、反斜杠混淆），所以由工具来拼。
    """
    raw = str(raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.exists():
        return p
    rel = raw.replace("\\", "/").lstrip("/")
    roots: list[Path] = []
    m = str(mount or "").replace("\\\\?\\", "").strip()
    if m:
        roots.append(Path(m))
    roots.extend(osmap_roots())
    for r in roots:
        try:
            c = r / rel
            if c.exists():
                return c
        except OSError:
            continue
    return None


def read_text_file(path: Path, max_bytes: int = 200_000,
                   pattern: str | None = None) -> str:
    """按常见中文/Windows 编码依次尝试读出文本，返回带行号的内容。

    Foxmail 的 Account.cfg 是 GBK、opencode.jsonc 是 UTF-8、
    部分导出文件是 UTF-16LE —— 一律硬解成 UTF-8 会得到一片乱码，
    然后用乱码去「核对证据」就必然出错。
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        raw = fh.read(min(size, max(1, max_bytes)))
    text, used = "", ""
    for enc in TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
            used = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not used:
        text = raw.decode("utf-8", "replace")
        used = "utf-8(replace)"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as e:
            return f"【参数错误】pattern 不是合法正则：{e}"
        keep = [(i + 1, ln) for i, ln in enumerate(lines) if rx.search(ln)]
        body = "\n".join(f"{i:>6}| {ln}" for i, ln in keep[:400]) or "（没有匹配行）"
        head = (f"文件：{path}\n大小：{size:,} 字节；编码：{used}；"
                f"读到 {len(raw):,} 字节；匹配 {len(keep)} 行 / 共 {len(lines)} 行\n"
                f"正则：{pattern}\n—— 匹配行 ——\n")
        return head + body
    shown = "\n".join(f"{i:>6}| {ln}" for i, ln in enumerate(lines[:600], 1))
    tail = "" if len(lines) <= 600 else f"\n…（还有 {len(lines)-600} 行，用 pattern 过滤）"
    return (f"文件：{path}\n大小：{size:,} 字节；编码：{used}；"
            f"读到 {len(raw):,} 字节；共 {len(lines)} 行\n—— 内容 ——\n{shown}{tail}")


# ---- 大文件流式扫描：给「file_too_large」这类墙准备的真通道 ----
# ges grep 在 /evidence_mounts 上有单文件 32 MiB 上限（后端常量，改不了参数绕过）。
# 而 ext4.vhdx / E01 这种动辄十几 GB 的东西，恰恰是「WSL 里装了什么」的唯一载体。
# 所以必须有一条**不整读进内存**的本地扫描通道：分块 + 重叠窗口 + 字节级 find，
# 同时按 UTF-8/ASCII 和 UTF-16LE 两种编码找（Windows/PE 里的字符串大量是宽字符）。
SCAN_MAGIC = [
    (b"vhdxfile", "VHDX 虚拟磁盘（Hyper-V / WSL2 的 ext4.vhdx）"),
    (b"conectix", "VHD 虚拟磁盘"),
    (b"EVF\x09\x0d\x0a\xff\x00", "EWF / E01 取证镜像"),
    (b"PK\x03\x04", "ZIP 容器（docx/apk/jar…）"),
    (b"\x7fELF", "ELF 可执行文件 / Linux 二进制"),
    (b"MZ", "PE 可执行文件（Windows）"),
]


def _printable(raw: bytes, wide: bool = False) -> str:
    """把窗口字节变成人能看的一行：控字符换空格、压掉连续空白。"""
    try:
        s = raw.decode("utf-16-le" if wide else "utf-8", "replace")
    except Exception:
        s = raw.decode("latin-1", "replace")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def probe_magic(head: bytes) -> str:
    """按文件头猜类型——扫错文件是最常见的一种白费力气。"""
    hits = [desc for sig, desc in SCAN_MAGIC if head.startswith(sig) or sig in head[:4096]]
    # ext4 的 0xEF53 在超级块偏移 0x438，不在文件头，单独认
    if len(head) > 0x43A and head[0x438:0x43A] == b"\x53\xef":
        hits.append("ext2/3/4 文件系统超级块")
    return "、".join(dict.fromkeys(hits)) or "未识别（按二进制处理）"


_HITSUM = re.compile(r"命中次数：([^\n]+)")
_HITZERO = re.compile(r"^命中：0", re.M)
_HITLINE = re.compile(r"^  \[\d+\] 偏移", re.M)


def _report_hits(rep: str) -> int:
    """从 scan_file_stream 的报告里取命中总数（0 也是有效结果）。"""
    if _HITZERO.search(rep):
        return 0
    m = _HITSUM.search(rep)
    if not m:
        return 0
    total = 0
    for seg in m.group(1).split("、"):
        if "=" in seg:
            try:
                total += int(seg.rsplit("=", 1)[1])
            except ValueError:
                pass
    return total


def classify_path(p: Path) -> str:
    """判断这个路径是「检材挂载视图」还是「本机磁盘」。

    用户经常直接给一个火眼/美亚挂出来的目录让我们读——那条路径在他自己电脑上，
    但内容属于检材。反过来，如果模型拿 `C:/Users/admin/.workbuddy/...` 去答，
    那读的就是**分析员自己的电脑**，跟案子毫无关系。
    这两个必须能分开，否则报告里会出现「证据来自本机」这种致命错误。
    """
    s = str(p).replace("/", "\\").lower()
    mount_marks = ("\\osmap\\", "\\hlnet\\", "\\evidence_mounts\\", "\\mount\\",
                   "\\case\\", "\\myos\\case\\")
    if any(m in s for m in mount_marks):
        return "检材挂载视图"
    return "本机磁盘（不属于检材，除非用户明确说这是导出目录）"


def list_tree(root: Path, depth: int = 2, include: str = "", max_items: int = 400,
              max_seconds: float = 60.0) -> str:
    """像 `ls -R` 一样列目录，不依赖任何 MCP。

    为什么必须有不靠 MCP 的浏览能力：用户把火眼挂载出来的目录一给，
    人眼就能顺着目录找可疑文件；但模型只会「扫关键词」——不知道关键词时就抓瞎。
    有了这一层，它才能像人一样先看结构、再决定读哪个文件。
    """
    pats = [x.strip().lower() for x in re.split(r"[,，]", include or "") if x.strip()]
    pats = [p if p.startswith(".") or "*" in p else "." + p for p in pats]
    if not root.is_dir():
        return f"【不是目录】{root} 是个文件。要看内容用 sys_file_read。"
    t0 = time.time()
    lines: list[str] = []
    dirs = files = 0
    truncated = False
    base_depth = len(root.parts)

    def _match(name: str) -> bool:
        if not pats:
            return True
        low = name.lower()
        return any(fnmatch.fnmatch(low, p) for p in pats)

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, d = stack.pop(0)
        if d >= depth:
            continue
        try:
            entries = sorted(os.scandir(cur), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            lines.append(f"  {'  ' * d}[无法读取] {cur.name or cur}  ({e.__class__.__name__})")
            continue
        for e in entries:
            if time.time() - t0 > max_seconds or len(lines) >= max_items:
                truncated = True
                break
            ind = "  " * d
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                dirs += 1
                lines.append(f"{ind}[D] {e.name}/")
                stack.append((Path(e.path), d + 1))
            else:
                if not _match(e.name):
                    continue
                files += 1
                try:
                    size = e.stat().st_size
                    lines.append(f"{ind}[F] {size:>12,} B  {e.name}")
                except OSError:
                    lines.append(f"{ind}[F] {'?':>12}  {e.name}")
        if truncated:
            break

    head = [f"目录：{root}",
            f"类型：{classify_path(root)}",
            f"深度：{depth}　子目录 {dirs} 个　文件 {files} 个"
            + (f"（已达上限截断）" if truncated else "（完整）")]
    if pats:
        head.append("后缀过滤：" + "、".join(pats))
    if not lines:
        head.append("（空目录，或没有符合后缀的文件）")
    return "\n".join(head + ["—— 结构 ——"] + lines)


def scan_tree_stream(root: Path, keywords: list[str], regex: str | None = None,
                     context: int = 60, max_hits: int = 24, max_files: int = 150,
                     max_seconds: float = 240.0, per_file_max_bytes: int | None = None,
                     include: str = "", recursive: bool = True,
                     ignore_case: bool = True) -> str:
    """在**目录**里逐文件扫关键词（sys_file_scan 传目录时走这条）。

    为什么要有它：模型最常见的死法就是「我知道账号在 AppData\\Roaming\\Foxmail7 下，
    但工具只收单文件」——于是它开始瞎猜文件名、或者直接交白卷。
    目录这一层必须由工具吃掉：遍历、过滤、限时、限数量、汇总。
    """
    import fnmatch

    pats = [s.strip() for s in re.split(r"[,，|]", include) if s.strip()]
    t0 = time.time()
    files: list[Path] = []
    skipped_big = 0
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = Path(dirpath) / fn
                if pats and not any(fnmatch.fnmatch(fn.lower(), g.lower()) for g in pats):
                    continue
                try:
                    if p.stat().st_size == 0:
                        continue
                except OSError:
                    continue
                files.append(p)
    else:
        for p in sorted(root.iterdir()):
            if p.is_file():
                try:
                    if p.stat().st_size:
                        files.append(p)
                except OSError:
                    continue

    files.sort()
    candidates = len(files)
    per_file: list[tuple[int, str]] = []
    hit_blocks: list[str] = []
    total = 0
    scanned = 0
    timed_out = False
    for p in files[:max_files]:
        if time.time() - t0 > max_seconds:
            timed_out = True
            break
        try:
            if per_file_max_bytes and p.stat().st_size > per_file_max_bytes:
                rep = scan_file_stream(p, keywords, regex=regex, context=context,
                                       max_hits=3, max_bytes=per_file_max_bytes,
                                       ignore_case=ignore_case, max_seconds=30)
                skipped_big += 1
            else:
                rep = scan_file_stream(p, keywords, regex=regex, context=context,
                                       max_hits=3, ignore_case=ignore_case,
                                       max_seconds=30)
        except Exception as e:                     # 权限/独占/坏文件都不该中断整趟
            per_file.append((-1, f"{p.relative_to(root)}  （读取失败：{type(e).__name__}）"))
            continue
        scanned += 1
        n = _report_hits(rep)
        if n:
            total += n
            per_file.append((n, str(p.relative_to(root))))
            if len(hit_blocks) < max_hits:
                for ln in rep.splitlines():
                    if _HITLINE.match(ln) and len(hit_blocks) < max_hits:
                        hit_blocks.append(f"{p.relative_to(root)}\n{ln}")

    el = time.time() - t0
    lines = [f"目录：{root}",
             f"候选文件 {candidates} 个，实扫 {scanned} 个，耗时 {el:.1f}s"
             + (f"（超过 {max_seconds:.0f}s 时间上限，剩余未扫）" if timed_out else "")
             + (f"（{skipped_big} 个超限文件只扫了前段）" if skipped_big else "")]
    if not pats:
        lines.append("过滤：无（全部文件）")
    else:
        lines.append("过滤：" + "、".join(pats))
    if not total:
        lines.append("命中：0 —— 这些文件都真读过，不是「没扫」。"
                     "若目标可能是二进制/压缩/数据库内部，改用裸数据检索或换关键词。")
    else:
        lines.append(f"命中合计 {total} 次，涉及 {len([x for x in per_file if x[0] > 0])} 个文件：")
        for n, rel in sorted(per_file, key=lambda x: -x[0])[:20]:
            lines.append(f"  [{n if n >= 0 else 'ERR'}] {rel}")
        lines.append("命中原文（前 %d 条）：" % len(hit_blocks))
        lines.extend("  " + b.replace("\n", "\n  ") for b in hit_blocks)
    if candidates > max_files:
        lines.append(f"注意：只扫了前 {max_files} 个文件（还有 {candidates - max_files} 个）。"
                     f"要更准就用 include 过滤后缀，或缩小目录。")
    return "\n".join(lines)


def scan_file_stream(path: Path, keywords: list[str], regex: str | None = None,
                     context: int = 90, max_hits: int = 40, chunk_mb: int = 8,
                     max_bytes: int | None = None, ignore_case: bool = True,
                     start: int = 0, max_seconds: float | None = None) -> str:
    """分块扫描任意大小的本地文件，返回命中报告（字节偏移 + 上下文）。

    设计取舍：
      · 不整读——18.5 GB 的文件整读进内存会直接把进程打死；
      · 重叠窗口取「最长模式 + 上下文字节」，保证跨块边界的命中不漏；
      · 两种编码都扫——`ollama` 在 PE/宽字符串里是 `o\\x00l\\x00l\\x00…`，
        只按 ASCII 找会得到「0 命中」这种最坑的假阴性；
      · **可续扫**：镜像走 dokan 挂载时读速只有几十 MB/s，18 GB 要跑十几分钟。
        工具调用不能挂十几分钟，所以按 max_seconds 切段返回 `resume_offset`，
        模型接着传 start 往下扫即可——分段扫描照样是「全量读过」，不是抽样。
    """
    size = path.stat().st_size
    begin = max(0, min(int(start or 0), size))
    end = size if max_bytes is None else min(size, begin + max(0, int(max_bytes)))
    chunk = max(1, chunk_mb) * 1024 * 1024
    ctx = max(20, min(400, context))
    deadline = (time.time() + max_seconds) if max_seconds else None

    # 模式：普通子串（两种编码）+ 可选正则（只按 UTF-8 字节流跑）
    pats: list[tuple[str, bytes, bool]] = []
    for kw in keywords:
        if not kw:
            continue
        for wide in (False, True):
            b = kw.encode("utf-16-le") if wide else kw.encode("utf-8", "replace")
            pats.append((kw, b.lower() if ignore_case else b, wide))
    maxpat = max((len(p[1]) for p in pats), default=8)
    overlap = min(chunk // 2, maxpat + ctx * 2 + 16)

    counts = {kw: 0 for kw in keywords}
    hits: list[str] = []
    rx = None
    if regex:
        try:
            rx = re.compile(regex.encode("utf-8", "replace"),
                            re.I if ignore_case else 0)
        except re.error as e:
            return f"【参数错误】regex 无法编译：{e}"

    t0 = time.time()
    scanned = begin
    tail = b""
    resume = None
    with path.open("rb") as fh:
        head = fh.read(8192)
        fh.seek(begin)
        while scanned < end:
            if deadline and time.time() > deadline:
                resume = scanned
                break
            block = fh.read(min(chunk, end - scanned))
            if not block:
                break
            buf = tail + block if tail else block
            base = scanned - len(tail)          # buf[0] 在文件里的绝对偏移
            scanned += len(block)
            # 模式已经小写化了，缓冲区也必须小写化后再比——否则 `OLLAMA`
            # 这种大写的字符串永远找不到，出来的是最坑的假阴性。
            # bytes.lower() 只动 ASCII 字母、长度不变，偏移可以直接用。
            low = buf.lower() if ignore_case else buf

            for kw, pat, wide in pats:
                if not pat:
                    continue
                pos = 0
                while True:
                    i = low.find(pat, pos)
                    if i < 0:
                        break
                    counts[kw] += 1
                    if len(hits) < max_hits:
                        off = base + i
                        w0 = max(0, i - (ctx * 2 if wide else ctx))
                        w1 = min(len(buf), i + len(pat) + (ctx * 2 if wide else ctx))
                        tag = "UTF-16LE" if wide else "ASCII/UTF-8"
                        hits.append(f"  [{len(hits)+1}] 偏移 {off:,} · {tag} · {kw}\n"
                                    f"        …{_printable(buf[w0:w1], wide)}…")
                    pos = i + 1

            if rx is not None:
                for m in rx.finditer(buf):
                    val = _printable(m.group(0))[:60]
                    counts.setdefault(val, 0)
                    counts[val] += 1
                    if len(hits) < max_hits:
                        off = base + m.start()
                        w0 = max(0, m.start() - ctx)
                        w1 = min(len(buf), m.end() + ctx)
                        hits.append(f"  [{len(hits)+1}] 偏移 {off:,} · REGEX · {val}\n"
                                    f"        …{_printable(buf[w0:w1])}…")

            tail = buf[-overlap:] if overlap else b""

    cost = time.time() - t0
    span = max(0, scanned - begin)
    pct = (span / (end - begin) * 100) if end > begin else 100.0
    lines = [
        f"文件：{path}",
        f"大小：{size:,} 字节；本次扫描：{begin:,} → {scanned:,}（{span:,} 字节，"
        f"覆盖本段 {pct:.1f}%"
        + ("，全量完成" if resume is None and end == size else "") + "）",
        f"类型线索：{probe_magic(head)}",
    ]
    found = {k: v for k, v in counts.items() if v}
    if not found:
        if resume is not None:
            msg = "本段确实没有；剩余部分还没扫到，别当成「不存在」"
        elif end < size:
            msg = (f"目标区间已全量读过，但后面还有 {size - end:,} 字节没扫——"
                   f"别把这一段 0 命中当成「整份文件没有」")
        else:
            msg = "这是**真·没找到**，不是没法扫：文件已全量读过"
        lines.append(f"命中：0（共 {len(pats)} 个模式，双编码）——{msg}")
    else:
        lines.append("命中次数：" + "、".join(f"{k}={v}" for k, v in found.items()))
        lines.append("命中原文（最多 %d 条）：" % max_hits)
        lines.extend(hits)
        if sum(found.values()) > max_hits:
            lines.append(f"  …共 {sum(found.values())} 次，只列前 {max_hits} 条；"
                         f"要更精确就把关键词写长一点。")
    if resume is not None:
        lines.append(f"⏱ 到达单次时长上限（{max_seconds:.0f}s），已停在 {resume:,}。"
                     f"**接着扫就再调一次，start={resume}**（分段扫描≠抽样，HOLD 住直到 resume 为 null）。")
    lines.append(f"耗时 {cost:.1f}s。")
    return "\n".join(lines)


# ---- 工具返回的分类：策略拒绝 ≠ 执行失败 ----
# 混在一起计数会出两个问题：1)「被安全闸拦住」被当成「取证软件没连上」，
# 给出完全错误的诊断；2) 模型换个写法重试时，三次就整轮停下了。
BLOCK_PREFIXES = ("【已拦截】", "【已拒绝】", "【不可用】", "【无法执行】", "【需确认】")
FAIL_PREFIXES = ("【命令失败", "【超时】", "【未找到技能】", "【工具异常】",
                 "【参数解析失败】", "【参数错误】", "【执行失败】")


def failure_kind(text: str) -> str:
    """把工具返回分成 blocked / failed / ''（正常）。"""
    if not isinstance(text, str) or not text.startswith("【"):
        return ""
    if text.startswith(BLOCK_PREFIXES):
        return "blocked"
    if text.startswith(FAIL_PREFIXES):
        return "failed"
    return ""


# 第 2 次同类失败时追加到 tool 结果末尾。必须写在 tool 消息里——
# 工具循环中间插 user 消息会让消息序列非法（assistant(tool_calls) 后必须紧跟 tool 响应）。
STEER_BLOCKED = (
    "\n\n【别再用这条命令】这是策略拒绝，不是软件没连上，也不是参数写错。"
    "两种常见原因：命令里带了 `|` `;` `&&` `>` 之类的 shell 控制符（一次只能跑一条），"
    "或者命令里出现了 import / open / write / set / run 这类「可能改数据」的动词，被当成写操作拦下。"
    "换写法、换引号都过不了这一关，请直接换路子：\n"
    "· 读镜像 / 插件库里的数据 → 用 MCP 工具（query_cases、load_case、sqlite_query、"
    "global_search_content、quick_search）\n"
    "· 要跑厂商 CLI → 照技能正文里写好的那条命令原样抄，一条一条发\n"
    "· 确实需要写操作 → 让用户打开右上角「允许写操作」，别自己反复试\n"
    "已经拿到的信息够就先给结论，不要在同一个思路上硬凿。"
)
STEER_FAILED = (
    "\n\n【换个思路】这已经是连续第 2 次失败了，不要再用同样的工具+同样的参数重试。"
    "先看一眼上面的报错原文：如果是「找不到 / 未连接 / 未就绪」，说明这台软件现在用不了，"
    "改用技能 CLI 或直接告诉用户；如果是「参数不对 / 未找到技能」，就按报错里给的可用项重写参数。"
)

# 第 3 次同类失败时不再整轮停下，而是**熔断这一个工具**：
# 它已经被证明在这台机器上跑不通，继续留在工具清单里只会诱导模型反复撞。
# 摘掉之后模型必须换路子，任务能继续；真的一个工具都不剩时它会自己收口。
FUSE_NOTE = (
    "\n\n【这个工具已停用】它在本次任务里连续失败 3 次，同样的环境再调一次也不会变。"
    "接下来请换别的工具或命令，或者用已经拿到的数据往下推进。"
    "如果确实只有它能做，就直接说明卡在哪、需要什么条件，不要再试它。"
)
FUSED_REPLY = (
    "【该工具已停用】{name} 在本次任务中已被熔断（原因：{why}）。"
    "这次调用没有执行，也不会再执行。请改用其它办法，或用现有数据推进。"
)

# 会改变案件 / 检材状态的工具 —— 默认必须人工确认
DESTRUCTIVE_TOOLS = {
    "myos-forensics.decrypt_partition",
    "myos-forensics.restart_computer_forensics",
    "myos-forensics.close_case",
    "myos-forensics.export_case",
    "myos-forensics.export_bcp",
    "myos-forensics.stop_task",
    "myos-forensics.stop_phone_forensics_task",
    "myos-forensics.reset_phone_detection",
    "myos-forensics.create_case_and_open",
    "myos-forensics.open_case",
    "myos-forensics.add_disk_material",
    "myos-forensics.add_image_material",
    "myos-forensics.add_file_collection_material",
    "myos-forensics.submit_computer_forensic_task",
    "myos-forensics.start_phone_direct_forensics_task",
    "myos-forensics.start_phone_file_forensics_task",
    "myos-forensics.start_phone_folder_forensics_task",
    "myos-forensics.start_auto_mobile_forensics",
    "myos-forensics.start_computer_image_emulation_task",
    "myos-forensics.create_file_index_task",
    "myos-forensics.open_mftool",
}

# 曾经被误判成「写操作」的只读工具。它们的语义都是「查询」，拦下来等于
# 把取证分析的主干道封死——load_case 不挂载就读不到任何节点，全文检索
# 不跑就挖不出聊天记录和浏览器历史。单独列出来免得以后又被塞回上面。
READ_ONLY_TASK_TOOLS = frozenset({
    "myos-forensics.load_case",              # 只读挂载，官方描述「多次调用无影响」
    "myos-forensics.global_search_content",  # 返回 taskId 的检索任务，不改数据
    "myos-forensics.raw_data_content_search",
})


# ==========================================================================
# 日志
# ==========================================================================
class Session:
    """一次会话的落盘记录（jsonl + 可选 markdown 报告）。

    文件**懒创建**，而且只对「实质内容」创建：说过话、调过工具才落盘。
    启动探活、MCP 握手这类系统事件如果发生在空会话里就直接丢弃 ——
    否则每起一次控制台都留一个空 jsonl，历史列表很快就全是空会话。
    文件一旦建出来，系统事件就照常记录了（复现问题有用）。
    """

    # 只有这些 kind 才值得为它开一个文件
    MATERIAL = frozenset({"user", "tool_call", "tool_blocked", "assistant_final",
                          "manual_run", "report_saved"})

    def __init__(self, enabled: bool = True, path=None):
        self.started = datetime.now()
        self.enabled = enabled
        self.path: Path | None = Path(path) if path else None
        if enabled and self.path is None:
            self.path = LOG_DIR / (self.started.strftime("%Y%m%d-%H%M%S") + ".jsonl")
        self._fh = None

    def _open(self):
        """拿到可写的文件句柄；已经打开过就直接复用。

        ⚠️ 这里以前是 `if self._fh: return None`，于是**只有第一条记录能落盘**
        （打开文件的那次），后面所有事件都被 `if not fh: return` 丢掉 ——
        表现就是「历史会话只有提问、没有答案」。改成复用现有句柄。
        """
        if not self.enabled or self.path is None:
            return None
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        return self._fh

    def log(self, kind: str, **payload):
        if self._fh is None and kind not in self.MATERIAL:
            return          # 空会话里的系统事件，不值得为它建文件
        fh = self._open()
        if not fh:
            return
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
        rec.update(payload)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    @property
    def id(self) -> str:
        return self.path.stem if self.path else ""

    def save_report(self, text: str) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stem = self.path.stem if self.path else self.started.strftime("%Y%m%d-%H%M%S")
        p = REPORT_DIR / (stem + "-report.md")
        p.write_text(text, encoding="utf-8")
        return p


# ==========================================================================
# 运行控制：中断 + 插话
# ==========================================================================
class RunControl:
    """给正在跑的那一轮开两个口子。

    - **中断**：置位后主循环在下一个检查点收手，不再往下跑工具。
    - **插话**：消息进队列，主循环每轮开头取一次，作为 user 消息塞进对话。
      之所以不在工具调用中途塞，是因为 assistant(tool_calls) 后面必须紧跟
      它自己的 tool 响应，中间夹一条 user 消息，OpenAI 系接口直接 400。

    两者都是**线程安全**的：Web 端在另一个请求线程里调用，主循环在
    对话线程里读。
    """

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._inbox: list[str] = []
        self.interrupted = ""      # 非空表示是手动中断，值为中断来源
        self.consumed = 0          # 已经被模型看到几条插话

    # ---- 中断 ----
    def stop(self, why: str = "user"):
        self.interrupted = why or "user"
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---- 插话 ----
    def push(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        with self._lock:
            self._inbox.append(t)
        return True

    def drain(self) -> list[str]:
        with self._lock:
            got, self._inbox = self._inbox, []
        self.consumed += len(got)
        return got

    def pending(self) -> int:
        with self._lock:
            return len(self._inbox)


# ==========================================================================
# MCP stdio 客户端
# ==========================================================================
class McpError(RuntimeError):
    pass


class McpClient:
    """最小可用的 MCP stdio 客户端（换行分隔 JSON-RPC 2.0）。"""

    def __init__(self, command: str, args=None, env=None, cwd=None, timeout=120):
        self.command = command
        self.args = list(args or [])
        self.timeout = timeout
        self.server_info = {}
        self.protocol_version = None
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._stderr_tail: list[str] = []
        self._closed = False

        full_env = os.environ.copy()
        if env:
            full_env.update({k: str(v) for k, v in env.items()})

        workdir = cwd or str(Path(command).parent)
        try:
            self.proc = subprocess.Popen(
                [command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=full_env,
                bufsize=0,
            )
        except FileNotFoundError:
            raise McpError(
                f"找不到 MCP 可执行文件：{command}\n"
                f"请确认对应软件已安装，或修改 config.json → mcpServers 里该服务器的 command"
            )
        except OSError as e:
            raise McpError(f"无法启动 MCP 进程：{e}")

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---------------- 底层 IO ----------------
    def _read_stdout(self):
        stream = self.proc.stdout
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot["msg"] = msg
                    slot["event"].set()
            elif isinstance(msg, dict) and "method" in msg and "id" in msg:
                # 服务端反向请求：这里不支持，回错误避免对方卡死
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": -32601, "message": "client does not support " + str(msg.get("method"))}})
        self._fail_all("MCP 通道已关闭（进程退出）")

    def _read_stderr(self):
        stream = self.proc.stderr
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-40]

    def _write(self, obj):
        if self.proc.poll() is not None:
            raise McpError("MCP 进程已退出：" + self.stderr_tail())
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise McpError(f"写入 MCP 失败：{e}\n{self.stderr_tail()}")

    def _fail_all(self, reason: str):
        self._closed = True
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["msg"] = {"error": {"code": -32000, "message": reason}}
            slot["event"].set()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail[-10:])

    def _request(self, method: str, params=None, timeout=None):
        with self._lock:
            self._id += 1
            rid = self._id
            slot = {"event": threading.Event(), "msg": None}
            self._pending[rid] = slot
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if not slot["event"].wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise McpError(f"等待 {method} 响应超时（{timeout or self.timeout}s）")
        msg = slot["msg"] or {}
        if "error" in msg:
            err = msg["error"]
            raise McpError(f"{method} 失败：{err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result", {})

    def _notify(self, method: str, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------------- 协议层 ----------------
    def initialize(self, client_name="myos-forensics-agent", client_version="1.0.0"):
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": client_version},
        })
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized")
        return result

    def list_tools(self):
        tools, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None, timeout=None):
        return self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


# ==========================================================================
# 多 MCP 连接池
# ==========================================================================
class McpServerHandle:
    """一个 MCP 服务器的运行态。

    连接失败**不抛异常**，只把状态标成 error —— 这样多服务器里挂掉一个，
    其余照常可用（取证现场很常见：火眼没开、IDA 没开，都不该拖垮整个智能体）。
    """

    def __init__(self, name: str, cfg: dict | None = None, default_timeout: int = 120):
        self.name = name
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", True))
        self.default_timeout = int(self.cfg.get("timeout", default_timeout))
        self.client: McpClient | None = None
        self.tools: list[dict] = []
        self.state = "idle"          # idle | connecting | ready | error | disabled
        self.error: str | None = None
        self.latency_ms: int | None = None
        self.dangerous = set(self.cfg.get("dangerousTools") or [])

    @property
    def ready(self) -> bool:
        c = self.client
        # _closed 一定要认：读线程发现进程退出后只置了这个标志，
        # 不看它的话 handle 会一直自称 ready，往死通道里写，模型收到一堆
        # 「通道已关闭」还以为重试就行，白白烧光轮数。
        return (self.state == "ready" and c is not None
                and not getattr(c, "_closed", False)
                and c.proc.poll() is None)

    @property
    def label(self) -> str:
        return str(self.cfg.get("label") or self.name)

    def _drop_client(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    def connect(self, force: bool = False) -> "McpServerHandle":
        if not self.enabled:
            self.state = "disabled"
            self.error = None
            return self
        if self.ready and not force:
            return self
        self._drop_client()
        self.tools = []
        self.state = "connecting"
        self.error = None
        t0 = time.time()
        try:
            cmd = self.cfg.get("command") or ""
            if not cmd:
                raise McpError("未配置 command（config.json → mcpServers.<名字>.command）")
            self.client = McpClient(
                command=cmd,
                args=self.cfg.get("args") or [],
                env=self.cfg.get("env") or {},
                cwd=self.cfg.get("cwd") or None,
                timeout=self.default_timeout,
            )
            self.client.initialize(client_name="forensics-agent", client_version="2.0.0")
            self.tools = self.client.list_tools()
            self.state = "ready"
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # 有些 server 会把整段 traceback 塞进 error message，截断免得刷屏
            if len(msg) > 360:
                msg = msg[:360].rstrip() + "…（错误信息已截断，完整内容见 logs/）"
            self.error = msg
            self.state = "error"
            self._drop_client()
        self.latency_ms = int((time.time() - t0) * 1000)
        return self

    def close(self):
        self._drop_client()
        if self.state != "disabled":
            self.state = "idle"

    def status(self) -> dict:
        si = (self.client.server_info if self.client else None) or {}
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.cfg.get("kind") or "mcp",
            "note": self.cfg.get("note") or "",
            "enabled": self.enabled,
            "state": self.state,
            "ready": self.ready,
            "error": self.error,
            "toolCount": len(self.tools),
            "tools": [t.get("name") for t in self.tools],
            "server": si.get("name"),
            "version": si.get("version"),
            "protocol": self.client.protocol_version if self.client else None,
            "latencyMs": self.latency_ms,
            "command": self.cfg.get("command"),
        }


# 这些字样代表「进程/管道没了」，不是工具本身报错——重连一次通常能救回来。
# 注意别把「等待 xxx 响应超时」算进来：那是工具真的在跑，重连只会把活儿打断。
_DISCONNECT_HINTS = ("通道已关闭", "进程已退出", "写入 MCP 失败", "Broken pipe",
                     "Connection reset", "已退出")


def _is_disconnect(err) -> bool:
    s = str(err)
    return any(h in s for h in _DISCONNECT_HINTS)


class McpPool:
    """管理多个 MCP 服务器：并发连接、按服务器路由调用、单独重连。"""

    def __init__(self, servers: dict | None = None, default_timeout: int = 120):
        self.handles: dict[str, McpServerHandle] = {
            name: McpServerHandle(name, cfg, default_timeout)
            for name, cfg in (servers or {}).items()
        }
        self.default_timeout = default_timeout
        self.connect_ms = 0

    # ---------------- 连接 ----------------
    def connect_all(self, per_server_timeout: int = 60) -> list[dict]:
        t0 = time.time()
        threads = []
        for h in self.handles.values():
            if not h.enabled:
                h.state = "disabled"
                continue
            th = threading.Thread(target=h.connect, daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=per_server_timeout)
        for h in self.handles.values():
            if h.state == "connecting":
                h.state = "error"
                h.error = f"连接超时（>{per_server_timeout}s），进程可能卡在启动握手"
        self.connect_ms = int((time.time() - t0) * 1000)
        return self.status()

    # ---------------- 查询 ----------------
    def ready(self) -> list[McpServerHandle]:
        return [h for h in self.handles.values() if h.ready]

    def all_tools(self) -> list[tuple[str, dict]]:
        out = []
        for h in self.ready():
            for t in h.tools:
                out.append((h.name, t))
        return out

    def get(self, name: str) -> McpServerHandle | None:
        return self.handles.get(name)

    # ---------------- 调用 ----------------
    def call(self, server: str, tool: str, args: dict | None = None, timeout: int | None = None):
        h = self.handles.get(server)
        if not h:
            raise McpError(f"未知 MCP 服务器：{server}（已配置：{list(self.handles)}）")

        # ---------- 断连自愈 ----------
        # 取证软件 D-Bus 断了几秒就恢复是常事。以前直接把「通道已关闭」当工具结果
        # 丢给模型，它只会一遍遍重试直到烧光轮数。这里先替它连一次再试。
        healed = False
        now = time.time()
        if not h.ready and h.enabled and now - getattr(h, "_last_try", 0) > 5:
            h._last_try = now
            h.connect(force=True)
            healed = True
        if not h.ready:
            raise McpError(
                f"[{server}] 当前不可用（{h.state}）：{h.error or '未连接'}\n"
                f"这个工具的调用不会成功，别再重复调它。请让用户检查取证软件是否已启动，"
                f"然后在「连接器」面板点重连。")

        try:
            return h.client.call_tool(tool, args or {}, timeout=timeout or h.default_timeout)
        except McpError as e:
            if healed or not _is_disconnect(e):
                raise
            # 调用途中才死的：再连一次，救得回来模型完全无感
            h.connect(force=True)
            if not h.ready:
                raise McpError(
                    f"[{server}] 连接已断开且自动重连失败：{h.error or e}\n"
                    f"继续调用不会有结果。请让用户检查取证软件状态，再去「连接器」面板手动重连。")
            return h.client.call_tool(tool, args or {}, timeout=timeout or h.default_timeout)

    def is_destructive(self, server: str, tool: str) -> bool:
        if tool in DESTRUCTIVE_TOOLS:
            return True
        h = self.handles.get(server)
        return bool(h and tool in h.dangerous)

    # ---------------- 运维 ----------------
    def reconnect(self, name: str | None = None) -> list[dict]:
        targets = [self.handles[name]] if name and name in self.handles else list(self.handles.values())
        for h in targets:
            h.connect(force=True)
        return self.status()

    def set_enabled(self, name: str, enabled: bool) -> dict | None:
        h = self.handles.get(name)
        if not h:
            return None
        h.enabled = enabled
        h.cfg["enabled"] = enabled
        if enabled:
            h.connect(force=True)
        else:
            h.close()
            h.state = "disabled"
            # 关掉的时候把上一次的错误清掉。否则状态里会同时出现
            # 「未启用」和一条历史报错，看着像「关了也坏」，容易被误读成软件没启动。
            h.error = None
        return h.status()

    def upsert(self, name: str, cfg: dict) -> dict:
        """新增或就地更新一台服务器。已存在的先断开，再按新配置连。"""
        old = self.handles.get(name)
        if old:
            old.close()
        h = McpServerHandle(name, cfg, self.default_timeout)
        self.handles[name] = h
        if h.enabled:
            h.connect(force=True)
        else:
            h.state = "disabled"
        return h.status()

    def remove(self, name: str) -> bool:
        h = self.handles.pop(name, None)
        if not h:
            return False
        h.close()
        return True

    def rename(self, old: str, new: str) -> bool:
        if old == new or old not in self.handles or new in self.handles:
            return False
        self.handles[new] = self.handles.pop(old)
        self.handles[new].name = new
        return True

    def status(self) -> list[dict]:
        return [h.status() for h in self.handles.values()]

    def close(self):
        for h in self.handles.values():
            h.close()


# --------------------------------------------------------------------------
# 智能体自带的「系统工具」—— 不属于任何 MCP，直接由 agent 执行
# --------------------------------------------------------------------------
BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sys_skill_list",
            "description": "按关键词检索技能，返回最匹配的 1~3 个（名字 + 摘要 + 标签）。"
                           "**全部技能索引已经在系统提示的「可用技能」章节里了**，不要用它浏览清单，"
                           "更不要为了「都了解一下」反复调用。只在关键词模糊、需要快速定位时才用，"
                           "拿到结果后**只 sys_skill_read 排在第一位的那一个**。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "关键词，例如：内存镜像、时间线、APK、仿真、数据库",
                    },
                    "vendor": {
                        "type": "string",
                        "description": "可选，按厂商/分组过滤：honglian（弘连）、meiya（美亚柏科），"
                                       "或分组名（电子取证 / 通用与工具 / 安全研究…，见系统提示里的分组标题）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 3，最大 10",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_skill_read",
            "description": "读取某个技能的完整说明（SKILL.md 全文 + 附带文件清单）。"
                           "确定要用某个技能前必须先读，否则你不知道它的正确调用顺序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，例如 ges-case-management"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_skill_exec",
            "description": "在某个技能的工作目录里执行一条命令（通常是该技能自带的 CLI，"
                           "如 ges / thunder-app-cli / bootmagix-cli）。一次只允许一条命令，"
                           "不允许管道、重定向、&& 等 shell 组合。会改动数据的命令需要人工确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名"},
                    "command": {"type": "string", "description": "要执行的完整命令，例如：ges status"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 300"},
                },
                "required": ["name", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_server_status",
            "description": "查看所有 MCP 服务器的连接状态、工具数量与失败原因。"
                           "某台服务器工具调不通时先调这个，不要盲目重试。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_server_reconnect",
            "description": "重连某个 MCP 服务器（不传名字则全部重连）。"
                           "服务器显示「未启用」时也会被自动打开（并写回配置），"
                           "所以工具调不通时先调这个，不要先去改配置文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "服务器名，留空表示全部重连"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_file_scan",
            "description": "在**本地磁盘文件或目录**里流式扫关键词（不限文件大小，不整读内存，"
                           "UTF-8 与 UTF-16LE 双编码都扫），返回命中次数、字节偏移和上下文原文。"
                           "path 传目录时会自动遍历（可 include 过滤后缀），"
                           "**所以「只知道大概在哪个目录、不知道具体文件名」时直接传目录**，"
                           "不要一个文件一个文件去试。"
                           "用途：ges grep 报 file_too_large / 单文件 32 MiB 上限，或者 .vhdx/.E01 "
                           "这类大二进制没法用普通工具读时，先用 ges copy 复制到本地，再用本工具扫；"
                           "也可用于镜像挂载视图（osmap 的 Root 目录）直接扫配置文件。"
                           "0 命中就是真没找到（文件已全量读过），不是「没法扫」。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "本地绝对路径（Windows 路径），文件或目录都可以，"
                                            "例如 E:/tmp/ext4.vhdx 或 "
                                            "C:/osmap/.../Root/Partition0_系统/Users/pc/AppData/Roaming"},
                    "keywords": {"type": "string",
                                 "description": "关键词，多个用英文逗号或竖线分隔，"
                                                "例如：ollama,llama.cpp,vllm,koboldcpp"},
                    "regex": {"type": "string",
                              "description": "可选，正则（与 keywords 二选一或叠加），"
                                             "例如 ollama[- ]?version|version\\s*0\\.[0-9]+"},
                    "context": {"type": "integer",
                                "description": "命中处上下文保留字符数，默认 90"},
                    "max_hits": {"type": "integer", "description": "最多列多少条原文，默认 40"},
                    "include": {"type": "string",
                                "description": "仅目录模式：按文件名通配过滤，逗号分隔，"
                                               "例如 *.ini,*.json,*.cfg,*.log；不填=全部文件"},
                    "max_files": {"type": "integer",
                                  "description": "仅目录模式：最多遍历多少个文件，默认 150"},
                    "recursive": {"type": "boolean",
                                  "description": "仅目录模式：是否递归子目录，默认 true"},
                    "max_bytes": {"type": "integer",
                                  "description": "本段最多扫多少字节（默认扫到文件末尾）；"
                                                 "目录模式下表示「超大文件只扫前 N 字节」"},
                    "start": {"type": "integer",
                              "description": "从第几个字节开始（续扫时用上次返回的 resume_offset）"},
                    "max_seconds": {"type": "number",
                                    "description": "单次最多跑多少秒，默认 240。超时会返回 "
                                                   "resume_offset，接着传 start 续扫"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_fs_index",
            "description": "在**检材的文件系统索引**（TBL_FS_ENTRY 表）里按文件名/路径查条目，"
                           "秒级返回 filepath、大小、是否目录。**查任何「某文件/某目录在哪」"
                           "都先用它**，不要一上来跑裸数据检索（那要几十分钟）。\n"
                           "典型用法：\n"
                           "· keyword=foxmail → 定位邮件客户端数据目录；\n"
                           "· keyword=account.cfg,prefs.js,*.ost → 找邮件账号配置文件；\n"
                           "· mode=dir + keyword=/Partition0_系统/Users/pc/ASR/models "
                           "→ 只列这个目录的直接子项（数模型个数就靠它）；\n"
                           "· keyword=config.json + ext=.json → 按后缀过滤。\n"
                           "拿到的 filepath 拼上挂载根（query_device_info 的 mount_path）就能直接读文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string",
                                "description": "文件名或路径关键词，多个用逗号（OR）；"
                                               "mode=dir 时是整个目录的 filepath"},
                    "mode": {"type": "string", "enum": ["any", "filename", "dir"],
                             "description": "any=文件名+路径都匹配（默认）；"
                                            "filename=只匹配文件名；"
                                            "dir=把 keyword 当目录，列它的直接子项"},
                    "ext": {"type": "string",
                            "description": "按后缀过滤，如 .ini,.json；留空不过滤"},
                    "case_root": {"type": "string",
                                  "description": "可选：案件根目录或 FSData.mfs 路径；"
                                                 "留空自动找最近修改的检材"},
                    "isfile": {"type": "boolean",
                               "description": "可选：true 只要文件，false 只要目录"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认 40"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_fs_list",
            "description": "列目录树（相当于 ls -R），**不依赖任何 MCP**。"
                           "用户给一个挂载出来的镜像目录（如火眼 C:/hlnet/.../Laptop_xxx.E01/系统/）时，"
                           "先用它看清结构，再决定读哪个文件。"
                           "和 sys_file_scan 的区别：scan 要你先想到关键词，list 不需要——"
                           "「不知道从哪下手」的时候用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "目录路径（本地路径或镜像内路径都行）"},
                    "depth": {"type": "integer", "description": "递归层数，默认 2"},
                    "include": {"type": "string",
                                "description": "只列符合的文件，如 *.py,*.json,*.md"},
                    "max_items": {"type": "integer", "description": "最多列多少条，默认 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_file_read",
            "description": "读一个**文本文件**的全文（自动识别 UTF-8 / GBK / UTF-16，"
                           "Windows 和中文软件的配置文件不会变乱码），可带 pattern 只返回匹配行。"
                           "**path 可以直接写镜像内路径**（sys_fs_index 返回的 filepath，"
                           "形如 /Partition0_系统/Users/pc/...），工具会自动拼上 osmap 挂载根；"
                           "也可以写本地绝对路径。"
                           "适合读 Account.cfg、prefs.js、*.json/jsonc、*.md、*.log、*.txt 这类小文件；"
                           "二进制大文件（.vhdx/.E01/.mfs）不要用它，用 sys_file_scan。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "镜像内路径或本地绝对路径"},
                    "pattern": {"type": "string",
                                "description": "可选正则，只返回匹配的行（带行号）"},
                    "max_bytes": {"type": "integer",
                                  "description": "最多读多少字节，默认 200000"},
                    "mount": {"type": "string",
                              "description": "可选：显式指定挂载根（query_device_info 的 mount_path）；"
                                             "留空自动找 C:/osmap/*/Root"},
                },
                "required": ["path"],
            },
        },
    },
]


def flatten_result(result: dict) -> str:
    """把 tools/call 结果压成一行可读文本，优先 structuredContent。"""
    if not isinstance(result, dict):
        return str(result)
    sc = result.get("structuredContent")
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if item.get("type") == "text" and item.get("text"):
                parts.append(item["text"])
            elif item.get("type") == "resource":
                parts.append(json.dumps(item.get("resource") or {}, ensure_ascii=False))
            elif item.get("type") and item["type"] != "text":
                parts.append(f"[{item['type']} 内容省略]")
    text = "\n".join(parts).strip()
    if sc is not None:
        sc_text = json.dumps(sc, ensure_ascii=False)
        if not text:
            text = sc_text
        elif len(sc_text) > len(text) * 1.5:
            # 文本层只是简短摘要、结构化字段更全时才补上，避免重复喂给模型
            text = text + "\n" + sc_text
    if result.get("isError"):
        text = "【工具报错】" + text
    return text or "（无返回内容）"


# ==========================================================================
# LLM 适配层（OpenAI 兼容）
# ==========================================================================
class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.2, timeout: int = 180, max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def chat(self, messages, tools=None, tool_choice="auto"):
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._post(body)
            except LlmError as e:
                last_err = e
                if attempt < self.max_retries and getattr(e, "retryable", False):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_err

    def _post(self, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "myos-forensics-agent/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:600]
            err = LlmError(f"模型接口 HTTP {e.code}：{detail}")
            err.retryable = e.code in (408, 429, 500, 502, 503, 504)
            raise err
        except urllib.error.URLError as e:
            err = LlmError(f"连不上模型接口 {self.endpoint}：{e.reason}")
            err.retryable = True
            raise err
        except Exception as e:
            raise LlmError(f"模型调用失败：{e}")

        # 接口偶尔会回一个空体 / 字面量 null / {"error":...}：
        # 直接 payload["choices"] 会抛 TypeError，而 TypeError 不在下面的
        # (KeyError, IndexError) 里，于是整轮对话被这样一个莫名其妙的
        # 「'NoneType' object is not subscriptable」炸掉——实测就炸过。
        if not isinstance(payload, dict):
            raise LlmError("模型返回的不是 JSON 对象：" + repr(payload)[:300])
        if payload.get("error") and not payload.get("choices"):
            err = LlmError("模型接口返回错误：" + json.dumps(payload["error"],
                                                        ensure_ascii=False)[:600])
            err.retryable = True
            raise err
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError("模型没有返回任何 choices：" + json.dumps(payload, ensure_ascii=False)[:600])
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(msg, dict):
            raise LlmError("模型返回的 message 不是对象：" + json.dumps(payload, ensure_ascii=False)[:600])
        return msg


# ==========================================================================
# 工具名清洗（OpenAI function name 只允许 [a-zA-Z0-9_-]）
# ==========================================================================
def sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


# DeepSeek 系模型偶尔会把原生工具调用标记当正文吐出来：
#   <｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="sys_skill_exec">…
# 这种内容进了正文就会污染报告和历史记录，还很容易被误当成「已经调用过」。


def digest_evidence(evidence: list[str], n: int = 4, width: int = 400) -> str:
    """收口时模型一个字都没写出来，就把**本轮真实拿到的工具返回**摆出来。

    以前的兜底是「共跑 N 轮、没能生成小结」——用户看到这句话只会觉得白跑，
    而实际上工具早就把答案查回来了。宁可摆原始结果，也不要让人从头再问一遍。
    """
    items = [str(e).strip() for e in (evidence or []) if str(e).strip()]
    if not items:
        return ""
    lines = ["（模型没能写出结论，下面是本轮**实际拿到的工具返回**，可直接核对）"]
    for i, t in enumerate(items[-n:], 1):
        flat = re.sub(r"\s+", " ", t)[:width]
        lines.append(f"{i}. {flat}")
    return "\n".join(lines)


def strip_tool_markup(text: str) -> str:
    """去掉正文里残留的工具调用标记（正常情况不该有）。

    这类标记是一整段序列化文本：标签行 + 参数内容行。只删标签会留下
    `python -c "xxx"` 这种断掉的参数行，看着更像乱码，所以整行一起丢。
    """
    if not text or "DSML" not in text.upper():
        return text
    keep = []
    for line in text.splitlines():
        if "DSML" in line.upper():
            continue
        keep.append(line)
    out = "\n".join(keep)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ==========================================================================
# 取证 Agent
# ==========================================================================
class ForensicsAgent:
    def __init__(self, cfg: dict, session: Session, auto_yes: bool = False,
                 allow_destructive: bool = False, verbose: bool = True,
                 max_tool_chars: int = 6000, skill_max_chars: int = 26000,
                 cfg_path=None):
        self.cfg = cfg
        # 配置文件位置。sys_server_reconnect 自动启用某台服务器后要写回这里，
        # 否则重启一次又变回「未启用」，用户就会觉得「怎么老是坏」。
        self.cfg_path = Path(cfg_path) if cfg_path else DEFAULT_CONFIG
        self.session = session
        self.auto_yes = auto_yes
        self.allow_destructive = allow_destructive
        self.verbose = verbose
        self.max_tool_chars = max_tool_chars
        self.skill_max_chars = skill_max_chars

        # ---------- 多 MCP 服务器 ----------
        servers_cfg = cfg.get("mcpServers")
        if servers_cfg is None:
            # 兼容 v1 单服务器配置（"mcp": {...}）
            legacy = cfg.get("mcp") or {}
            servers_cfg = {"myos-forensics": legacy} if legacy.get("command") else {}
        tool_timeout = int(cfg.get("tool_timeout", 300))
        self.pool = McpPool(servers_cfg, default_timeout=tool_timeout)
        self.pool.connect_all()
        self.tools, self._routes = self._collect_tools()

        # ---------- 技能库 ----------
        sc = cfg.get("skills") or {}
        # 相对路径一律按项目根解析。原来直接交给 expanduser，
        # 「./skills」会跟着启动时的 cwd 走——换个目录启动就静默少了 25 个技能。
        roots = []
        for r in (sc.get("roots") or []):
            p = Path(os.path.expandvars(os.path.expanduser(str(r))))
            if not p.is_absolute():
                p = (BASE_DIR / p)
            roots.append(p.resolve())
        allow = list(sc.get("allow_commands") or [])
        self.skills = SkillRegistry(roots, allow).scan()
        self._skill_reads: dict[str, int] = {}    # 技能名 → 已读次数
        # 当前这一轮的中断开关。run() 进来时挂上，出去时摘掉。
        # 挂在实例上是为了让 _call_builtin 里的长命令（sys_skill_exec）
        # 也能看见「用户点了中断」，从而提前杀子进程。
        self._control = None

        self.session.log(
            "agent_ready",
            servers=[h.status() for h in self.pool.handles.values()],
            tool_count=len(self.tools),
            skills=self.skills.summary(),
        )

    # ---------------- 工具收集 ----------------
    def _collect_tools(self):
        """把各服务器的工具摊平成一张表，并建立「清洗名 → (服务器, 真名)」路由。

        不同服务器可能有同名工具，撞名时优先用原始名，其次退回 `<服务器>_<原名>`。
        """
        tools, routes = [], {}
        for server, t in self.pool.all_tools():
            real = t.get("name") or ""
            if not real:
                continue
            key = sanitize(real)
            if key in routes:
                key = sanitize(f"{server}_{real}")
            n = 2
            while key in routes:
                key = sanitize(f"{server}_{real}_{n}")
                n += 1
            routes[key] = (server, real)
            item = dict(t)
            item["_server"] = server
            item["_key"] = key
            tools.append(item)
        return tools, routes

    def refresh_tools(self) -> int:
        """把工具表跟**当前**连接池对齐，返回工具数。

        连接池是活的：用户随时可能在界面上拨开关、服务器也可能自己重连成功，
        而 `self.tools` 是构造时的快照。快照过期就会出现最尴尬的一种「瞎说」——
        池里明明 49 个 ready，提示词里写着「✅ 49 个工具」却又跟一句「工具总数 0」，
        模型于是照实汇报「服务器被禁用、可用工具 0」，用户看到的就是模型在否认事实。

        每轮 run 开头对齐一次（纯内存操作，不发网络请求），成本几乎为零。
        """
        self.tools, self._routes = self._collect_tools()
        return len(self.tools)

    # ---------------- 工具 ----------------
    def tool_schemas(self):
        out = []
        for t in self.tools:
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            desc = (t.get("description") or "").strip()
            out.append({
                "type": "function",
                "function": {
                    "name": t["_key"],
                    "description": f"[{t['_server']}] {desc}"[:1200],
                    "parameters": schema,
                },
            })
        out.extend(BUILTIN_TOOLS)
        return out

    def resolve(self, name: str) -> tuple[str, str]:
        """返回 (服务器名, 真实工具名)。内置 sys_* 工具返回 ("__sys__", name)。"""
        if name in self._routes:
            return self._routes[name]
        for key, route in self._routes.items():
            if route[1] == name:
                return route
        # 容忍模型把前缀写错（点/下划线混用）
        norm = re.sub(r"[^a-z0-9]", "", name.lower())
        for key, route in self._routes.items():
            if re.sub(r"[^a-z0-9]", "", key.lower()) == norm:
                return route
        raise McpError(f"未知工具：{name}（可用工具 {len(self.tools)} 个，先看 sys_server_status）")

    def tool_index(self) -> list[dict]:
        """给 UI：工具 + 所属服务器。"""
        return [
            {
                "name": t.get("name"),
                "key": t["_key"],
                "server": t["_server"],
                "description": t.get("description") or "",
                "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
                "dangerous": self.pool.is_destructive(t["_server"], t.get("name")),
            }
            for t in self.tools
        ]

    def _confirm(self, name: str, args: dict) -> bool:
        if name not in DESTRUCTIVE_TOOLS:
            return True
        if self.auto_yes:
            return True
        if not self.allow_destructive:
            return False
        if not sys.stdin.isatty():
            return False
        print(f"\n  ⚠ 需要确认：{name}")
        print(f"    参数：{json.dumps(args, ensure_ascii=False)[:400]}")
        try:
            ans = input("    允许执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    def call(self, name: str, args: dict, quiet: bool = False, on_event=None, log_it: bool = True):
        """调一个工具。

        log_it=False 用于「界面自己拉数据」这类内部调用（比如侧栏的案件列表）——
        它们不该在会话日志里留下 tool_call，否则光打开页面就会凭空多出一堆
        「手动调用 N 个工具」的空会话。
        """
        args = args or {}

        # ---------- 内置系统工具（技能 / 服务器运维） ----------
        if name.startswith("sys_"):
            t0 = time.time()
            out = self._call_builtin(name, args, quiet, on_event)
            # 内置工具同样要落盘。以前只写 MCP 的，于是「它到底跑没跑那条 ges 命令」
            # 在历史里查不到 —— 用户没法核对，模型编起来也没痕迹。
            if log_it:
                self.session.log("tool_call", tool=name, server="__sys__", args=args,
                                 elapsed=round(time.time() - t0, 2),
                                 result=str(out)[:4000])
            return out

        server, real = self.resolve(name)

        if self.pool.is_destructive(server, real) and not self._confirm(real, args):
            msg = (f"【已拦截】{real} 属于会改动案件/检材状态的操作，未获人工授权，本次未执行。"
                   f"确需执行请开启「允许写操作」或在命令行加 --yes。")
            if log_it:
                self.session.log("tool_blocked", tool=real, server=server, args=args)
            if on_event:
                on_event({"type": "tool_blocked", "name": real, "server": server, "args": args})
            if not quiet:
                print(f"  ⊘ 拦截 {real}")
            return msg

        t0 = time.time()
        if not quiet:
            print(f"  → [{server}] {real} {json.dumps(args, ensure_ascii=False)[:180]}")
        if on_event:
            on_event({"type": "tool_start", "name": real, "server": server, "args": args})
        try:
            result = self.pool.call(server, real, args,
                                    timeout=int(self.cfg.get("tool_timeout", 300)))
            text = flatten_result(result)
        except McpError as e:
            text = f"【工具异常】{e}"
            h = self.pool.get(server)
            if h and h.client and h.client.stderr_tail():
                text += "\nstderr: " + h.client.stderr_tail()
            if h and not h.ready:
                text += (f"\n[{server}] 当前状态 {h.state}。若对方软件刚启动，"
                         f"可调 sys_server_reconnect 重连后再试。")
        elapsed = time.time() - t0
        if log_it:
            self.session.log("tool_call", tool=real, server=server, args=args,
                             elapsed=round(elapsed, 2), result=text[:4000])
        if not quiet:
            head = text.replace("\n", " ")[:220]
            print(f"  ← [{elapsed:.1f}s] {head}{'…' if len(text) > 220 else ''}")
        if on_event:
            on_event({"type": "tool_end", "name": real, "server": server,
                      "elapsed": round(elapsed, 2),
                      "preview": text.replace("\n", " ")[:200],
                      "full": text[:8000],
                      "error": text.startswith("【")})
        if len(text) > self.max_tool_chars:
            text = text[: self.max_tool_chars] + f"\n…（结果过长已截断，原长 {len(text)} 字符）"
        return text

    # ---------------- 内置工具实现 ----------------
    # ---------- 文件系统索引查询 ----------
    def _sqlite_tool(self) -> tuple[str, str] | None:
        """找一个能跑 SQL 的 MCP 工具。

        厂商主库 FSData.mfs 是加密的，直连 sqlite3 只会得到
        "file is not a database"，只有它们的 sqlite_query 带解密。
        """
        try:
            for server, t in self.pool.all_tools():
                n = str(t.get("name") or "")
                if n.endswith("sqlite_query"):
                    # pool.call 要的是带服务器前缀的全名
                    real = n if n.startswith(server + ".") else f"{server}.{n}"
                    return server, real
        except Exception:
            pass
        return None

    def _fs_index(self, args: dict) -> str:
        kw_raw = str(args.get("keyword") or "")
        kws = [k.strip() for k in re.split(r"[,，]", kw_raw) if k.strip()]
        if not kws:
            return ("【参数错误】keyword 为空。给文件名关键词（foxmail / account.cfg / "
                    "config.json），或 mode=dir 时给整个目录的 filepath。")
        mode = str(args.get("mode") or "any").lower()
        limit = max(1, min(300, int(args.get("limit") or 40)))
        exts = [e if e.startswith(".") else "." + e
                for e in (x.strip().lower() for x in re.split(r"[,，]", str(args.get("ext") or "")))
                if e]
        want_file = args.get("isfile")

        dbs = find_case_dbs(str(args.get("case_root") or ""))
        if not dbs:
            return ("【没找到检材索引】没定位到 FSData.mfs。先用 query_materials 查检材清单，"
                    "拿到检材目录后把 case_root 传成 `.../dev/<检材名>/data/FSData.mfs`。")
        db = dbs[0]
        others = dbs[1:4]

        def _esc(v: str) -> str:
            """转义 LIKE 通配符与单引号，供自己拼通配符时用。"""
            return (str(v).replace("\\", "\\\\").replace("%", "\\%")
                    .replace("_", "\\_").replace("'", "''"))

        def _like(v: str, wrap: bool = True) -> str:
            core = _esc(v)
            return "'" + (f"%{core}%" if wrap else core) + "'"

        where: list[str] = []
        if mode == "dir":
            pe = _esc(kws[0].replace("\\", "/").rstrip("/"))
            where.append(f"(filepath like '{pe}%' escape '\\' "
                         f"or filepath like '%{pe}%' escape '\\')")
            where.append(f"filepath not like '{pe}/%/%' escape '\\'")
        elif mode == "filename":
            where.append(" or ".join(
                f"lower(filename) like {_like(k.lower())} escape '\\'" for k in kws))
        else:
            where.append(" or ".join(
                f"(lower(filename) like {_like(k.lower())} escape '\\' "
                f"or lower(filepath) like {_like(k.lower())} escape '\\')" for k in kws))
        if mode == "dir":
            wsql = " and ".join(where)          # 目录前缀 + 只取直接子级
        else:
            wsql = "(" + " or ".join(where) + ")"   # 多个关键词是 OR
        if exts:
            alt = " or ".join(
                f"lower(filename) like {_like(e)} escape '\\'" for e in exts)
            wsql += f" and ({alt})"
        if want_file is True:
            wsql += " and isfile=1"
        elif want_file is False:
            wsql += " and isfile=0"

        cols = ["filename", "filepath", "filesize", "isfile", "extendname"]
        rows: list[list] = []
        how = ""
        tool = self._sqlite_tool()
        if tool:
            server, real = tool
            try:
                tdump = self.pool.call(server, real, {
                    "dbPath": str(db),
                    "sql": "select name from sqlite_master where type='table' "
                           "and name like 'TBL_FS_ENTRY%' and name not like '%_virtual%'",
                    "maxRows": 20}, timeout=90)
                td = json.loads(flatten_result(tdump))
                tables = [r[0] for r in (td.get("rows") or [])]
                tbl = pick_entry_table(tables)
                if tbl:
                    # 不要 order by：命中集可能上万行，排序白等十几秒；
                    # limit 之后顺序对定位没影响。
                    sql = (f"select {', '.join(cols)} from {tbl} where {wsql} "
                           f"limit {limit}")
                    res = self.pool.call(server, real,
                                         {"dbPath": str(db), "sql": sql, "maxRows": limit},
                                         timeout=180)
                    d = json.loads(flatten_result(res))
                    got_cols = [str(c) for c in (d.get("columns") or cols)]
                    rows = [list(r) for r in (d.get("rows") or [])]
                    how = f"sqlite_query（{server}）· 表 {tbl}"
            except Exception as e:
                how = f"MCP 取数失败（{type(e).__name__}: {str(e)[:80]}），改走明文索引"

        if not rows and not how.startswith("sqlite_query"):
            # ---- 退路：明文 FSDataIndex.mfs（只有文件名 + nodeId，但能证明「存在」）----
            idx = db.parent / "FSDataIndex.mfs"
            if idx.is_file():
                hits = plain_index_search(idx, kws, limit)
                if hits:
                    lines = [f"（厂商主库打不开，改用明文索引 {idx.name}：只有文件名 + nodeId，"
                             f"**没有全路径**）"]
                    for i, (n, nid) in enumerate(hits, 1):
                        lines.append(f"  [{i}] {n}  → nodeId {nid}")
                    lines.append("拿 nodeId 交给 query_current_node_data 取路径，"
                                 "或直接按文件名猜目录后传 sys_file_scan 目录模式。")
                    return "\n".join(lines)
            return (f"【0 命中】{db} 里没有匹配 {kw_raw} 的条目。\n"
                    f"这不等于「没有」——换短一点的关键词（foxmail 而不是 Foxmail7），"
                    f"或 mode=dir 列上级目录看看实际命名。")

        if not rows:
            return (f"【0 命中】表查询无结果（{how or '未知通道'}）。"
                    f"换更短的关键词，或用 mode=dir 列上级目录确认实际命名。")

        seen: set[str] = set()
        lines = [f"查的是：{db}",
                 f"通道：{how or '未知'} · 命中 {len(rows)} 条（关键词 {kw_raw}）"]
        if len(rows) >= limit:
            lines.append(f"（已取满 limit={limit}，说明还有更多：关键词太泛，"
                         f"换成更具体的名字能又快又准）")
        for i, r in enumerate(rows, 1):
            d = dict(zip(cols, (list(r) + [None] * len(cols))[:len(cols)]))
            fp = str(d.get("filepath") or "")
            if fp in seen:
                continue
            seen.add(fp)
            is_dir = d.get("isfile") in (0, "0", False, "false")
            size = d.get("filesize") or 0
            try:
                size = f"{int(size):,} B"
            except Exception:
                size = str(size)
            lines.append(f"  [{i}] {'目录' if is_dir else '文件'} · {size:>14} · {fp}")
        lines.append("提示：filepath 是镜像内路径，拼上挂载根（query_device_info 的 mount_path）"
                     "即可用 sys_file_read / sys_file_scan 直读内容。")
        if others:
            lines.append("另有检材索引：" + "、".join(str(o) for o in others)
                         + "（不对就用 case_root 指定）")
        return "\n".join(lines)

    def _call_builtin(self, name: str, args: dict, quiet: bool = False, on_event=None) -> str:
        def emit(ev):
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

        t0 = time.time()
        emit({"type": "tool_start", "name": name, "server": "__sys__", "args": args})

        if name == "sys_skill_list":
            kw = str(args.get("keyword") or args.get("query") or args.get("q") or "").strip()
            vendor = (args.get("vendor") or "").strip()
            try:
                limit = max(1, min(10, int(args.get("limit") or 3)))
            except (TypeError, ValueError):
                limit = 3

            if kw:
                rows = self.skills.search(kw, limit=limit)
                if vendor:
                    rows = [r for r in rows if r.get("vendor") == vendor]
                if rows:
                    text = json.dumps(rows, ensure_ascii=False, indent=2)
                else:
                    text = (f"没有匹配「{kw}」的技能。技能索引见系统提示的「可用技能」章节，"
                            f"直接从里面挑一个 read 即可，不要继续换关键词试探。")
            elif vendor:
                rows = self.skills.list(vendor)
                text = json.dumps(rows[:limit], ensure_ascii=False, indent=2)
            else:
                # 全量清单已经在系统提示里了，再吐一遍纯属浪费上下文，
                # 而且会鼓励模型「先 list 再挨个 read」这种烧轮数的方式。
                sm = self.skills.summary()
                lines = [f"技能索引已经在系统提示的「可用技能」章节里，不必重复获取。当前共 {sm['count']} 个："]
                for title, n in (sm.get("vendors") or {}).items():
                    lines.append(f"  · {title}：{n} 个")
                for rd in sm.get("rootDetail") or []:
                    mark = "✓" if rd["exists"] else "✗ 目录不存在"
                    lines.append(f"  目录 {rd['order']}. {rd['path']} → {rd['count']} 个 {mark}")
                lines.append("要定位具体技能就传 keyword 检索（一次拿到最匹配的几个），"
                             "然后只 sys_skill_read 第一位那一个。")
                text = "\n".join(lines)
                if not sm["count"]:
                    text = "本机未发现任何技能。请检查 config.json → skills.roots"
            if not quiet:
                print(f"  ← 技能 {len(self.skills.skills)} 个")

        elif name == "sys_skill_read":
            sk_name = (args.get("name") or args.get("skill") or "").strip()
            hits = self._skill_reads.get(sk_name, 0)
            if hits:
                # 同一轮会话里再读一遍纯属浪费上下文——但也不能让模型「失忆」，
                # 所以给回一段开头 + 指向上一份完整结果的提示。
                sk = self.skills.get(sk_name)
                head = (sk.body[:1500] + "…") if sk else ""
                text = (f"【已读过】技能「{sk_name}」本次会话前面已经读过 {hits} 次，"
                        f"完整正文就在上文那条工具结果里，往下照做即可，不要再重复读取。\n"
                        f"（下面是开头片段，仅供你回忆主题）\n{head}")
            else:
                text = self.skills.read(sk_name)
                if not text.startswith("【"):
                    self._skill_reads[sk_name] = 1
            if not quiet:
                print(f"  ← 读取技能 {sk_name}")

        elif name == "sys_skill_exec":
            sk_name = (args.get("name") or args.get("skill") or "").strip()
            cmd = (args.get("command") or "").strip()
            timeout = int(args.get("timeout") or 300)
            sk = self.skills.get(sk_name)
            if not sk:
                text = (f"【未找到技能】{sk_name}。注意：**技能名和它带的 CLI 名通常不一样**"
                        f"（技能叫 ges-case-analysis，命令叫 ges）——直接把 CLI 名当 name 传也可以。"
                        f"实在不确定就用 sys_skill_list 查一次。")
            elif not cmd:
                text = "【参数错误】command 为空。sys_skill_exec 需要一条完整命令，例如 ges status。"
            else:
                write_cmd = self.skills.is_write_command(cmd)
                if write_cmd and not self._confirm(f"{sk_name}: {cmd}", {"command": cmd}):
                    self.session.log("skill_blocked", skill=sk_name, command=cmd)
                    emit({"type": "tool_blocked", "name": f"{sk_name}: {cmd}",
                          "server": "__sys__", "args": {"command": cmd}})
                    text = (f"【已拦截】这条命令看起来会改动数据（{cmd}），未获授权未执行。"
                            f"这是安全闸拦的，不是你参数写错了。"
                            f"只想读数据的话改用 MCP 工具（query_cases / load_case / sqlite_query 等）；"
                            f"确需执行写操作，请让用户打开右上角「允许写操作」再重试，"
                            f"不要反复发同一条命令。")
                else:
                    if not quiet:
                        print(f"  → [skill:{sk_name}] {cmd}")
                    ctl = self._control
                    rc, text = self.skills.exec(
                        sk_name, cmd, timeout=timeout,
                        should_stop=(ctl.stopped if ctl else None))
                    self.session.log("skill_exec", skill=sk_name, command=cmd,
                                     rc=rc, result=text[:4000])

        elif name == "sys_file_scan":
            # 大文件通道：ges grep 在 /evidence_mounts 上有单文件 32 MiB 上限，
            # 而 ext4.vhdx / E01 动辄十几 GB——「拒绝当借口」的唯一办法是给它真手段。
            raw_path = str(args.get("path") or "").strip().strip('"').strip("'")
            if not raw_path:
                text = "【参数错误】path 为空。要扫的本地文件路径，例如 E:/tmp/ext4.vhdx"
            else:
                p = resolve_image_path(raw_path, str(args.get("mount") or ""))
                kw_raw = args.get("keywords") or ""
                if isinstance(kw_raw, str):
                    kws = [k.strip() for k in re.split(r"[,，|]", kw_raw) if k.strip()]
                else:
                    kws = [str(k).strip() for k in (kw_raw or []) if str(k).strip()]
                rx = (args.get("regex") or "").strip() or None
                if not kws and not rx:
                    text = "【参数错误】keywords 和 regex 至少要给一个。"
                elif p is None:
                    text = (f"【路径不存在】{raw_path} 既不是本地路径，也没在挂载视图里找到。\n"
                            f"先用 sys_fs_index 拿准确 filepath（形如 /Partition0_系统/Users/pc/...），"
                            f"它会自动拼上 osmap 挂载根；确实要复制出来就用 `ges copy <虚拟路径>`。")
                elif p.is_dir():
                    # 目录是一等公民：模型经常只知道「账号在 AppData\Roaming\Foxmail7 下」，
                    # 却拿不到具体文件名。让工具自己遍历，别逼它去猜文件名。
                    try:
                        text = scan_tree_stream(
                            p, kws, regex=rx,
                            context=int(args.get("context") or 60),
                            max_hits=max(1, min(60, int(args.get("max_hits") or 24))),
                            max_files=max(1, min(2000, int(args.get("max_files") or 150))),
                            max_seconds=float(args.get("max_seconds") or 240),
                            per_file_max_bytes=(int(args["max_bytes"])
                                                if args.get("max_bytes") else None),
                            include=str(args.get("include") or ""),
                            recursive=(args.get("recursive") is not False),
                        )
                    except Exception as e:
                        text = f"【目录扫描失败】{type(e).__name__}: {e}"
                elif not p.is_file():
                    text = (f"【不是文件】{p} 既不是普通文件也不是目录。\n"
                            f"确认一下路径：镜像内路径交给 sys_fs_index 查，"
                            f"本地大文件（已 ges copy 出来的）再传进来。")
                else:
                    try:
                        text = scan_file_stream(
                            p, kws, regex=rx,
                            context=int(args.get("context") or 90),
                            max_hits=max(1, min(200, int(args.get("max_hits") or 40))),
                            chunk_mb=int(args.get("chunk_mb") or 8),
                            max_bytes=(int(args["max_bytes"])
                                       if args.get("max_bytes") else None),
                            start=int(args.get("start") or 0),
                            max_seconds=float(args.get("max_seconds") or 240),
                        )
                    except Exception as e:
                        text = f"【扫描失败】{type(e).__name__}: {e}"
            # 同上：标明读的是检材还是分析员的电脑
            if p is not None and not str(text).startswith("【"):
                text = f"路径类型：{classify_path(p)}\n{text}"
            if not quiet:
                print(f"  ← 扫描 {raw_path}")

        elif name == "sys_fs_index":
            try:
                text = self._fs_index(args)
            except Exception as e:
                text = f"【索引查询失败】{type(e).__name__}: {e}"
            if not quiet:
                print(f"  ← 索引 {args.get('keyword')}")

        elif name == "sys_fs_list":
            raw_path = str(args.get("path") or "").strip().strip('"').strip("'")
            if not raw_path:
                text = "【参数错误】path 为空。给一个目录，例如用户提供的镜像挂载目录。"
            else:
                p = resolve_image_path(raw_path, str(args.get("mount") or ""))
                if p is None:
                    text = (f"【目录不存在】{raw_path}。\n"
                            f"如果是用户刚给的挂载路径，原样传进来（不要自己拼盘符或加 Root）；"
                            f"如果是镜像内路径，先用 sys_fs_index 查，或先 load_case 挂载。")
                elif not p.is_dir():
                    text = f"【不是目录】{p} 是个文件。读内容用 sys_file_read。"
                else:
                    try:
                        text = list_tree(
                            p,
                            depth=max(1, min(6, int(args.get("depth") or 2))),
                            include=str(args.get("include") or ""),
                            max_items=max(20, min(2000, int(args.get("max_items") or 400))),
                        )
                    except Exception as e:
                        text = f"【列目录失败】{type(e).__name__}: {e}"
            if not quiet:
                print(f"  ← 列目录 {raw_path}")

        elif name == "sys_file_read":
            raw_path = str(args.get("path") or "").strip().strip('"').strip("'")
            if not raw_path:
                text = "【参数错误】path 为空。"
            else:
                p = resolve_image_path(raw_path, str(args.get("mount") or ""))
                if p is None:
                    text = (f"【文件不存在】{raw_path} 既不是本地路径，也没能在挂载视图里找到。\n"
                            f"先用 sys_fs_index 拿到准确 filepath（形如 /Partition0_系统/Users/pc/...），"
                            f"或先用 load_case 挂载检材。")
                elif not p.is_file():
                    text = (f"【文件太大】{p.stat().st_size:,} 字节。这个工具是给配置文件用的；"
                            f"大文件请用 sys_file_scan 配 keywords / regex。")
                else:
                    try:
                        text = read_text_file(
                            p,
                            max_bytes=int(args.get("max_bytes") or 200_000),
                            pattern=(args.get("pattern") or None),
                        )
                        # 必须标明这个文件属于检材还是分析员自己的电脑。
                        # 以前不标，模型会把宿主机上的配置当成镜像里的证据写进结论。
                        text = f"路径类型：{classify_path(p)}\n{text}"
                    except Exception as e:
                        text = f"【读取失败】{type(e).__name__}: {e}"
            if not quiet:
                print(f"  ← 读取 {raw_path}")

        elif name == "sys_server_status":
            st = self.pool.status()
            lines = []
            for s in st:
                icon = {"ready": "OK", "error": "FAIL", "disabled": "OFF",
                        "connecting": "…", "idle": "IDLE"}.get(s["state"], s["state"])
                lines.append(
                    f"[{icon}] {s['name']}（{s['label']}）  state={s['state']}  "
                    f"tools={s['toolCount']}  {s['server'] or ''} {s['version'] or ''}".rstrip()
                )
                if s["error"]:
                    lines.append(f"       错误：{s['error']}")
                if s["state"] == "disabled":
                    lines.append("       未启用 = 有人把「启用」开关关掉了（不是掉线）。"
                                 "调 sys_server_reconnect 会自动把它打开并写回配置，"
                                 "不需要改 config.json、不需要重启客户端、也不需要用户动手。")
            if st and all(s["state"] == "disabled" for s in st):
                lines.insert(0, "注意：当前所有服务器都是「未启用」（开关被关掉了），不是软件没启动、"
                                "也不是连接失败。正在查案子就直接调 sys_server_reconnect，"
                                "它会自动开启并立刻拿到工具；只有重连后仍是 error 才是软件没开。"
                                "反正别让用户去改配置文件或重启客户端。")
            text = "\n".join(lines) or "未配置任何 MCP 服务器。"

        elif name == "sys_server_reconnect":
            target = (args.get("name") or "").strip() or None
            if target and target not in self.pool.handles:
                text = f"【未知服务器】{target}。已配置：{list(self.pool.handles)}"
            else:
                targets = [target] if target else list(self.pool.handles)
                # 「未启用」以前是死胡同：重连对关闭的开关无效，只能请人去点。
                # 但用户问的是问题，不是配置——重连本身就意味着「我要它可用」，
                # 所以这里顺手把开关打开并写回配置，一条命令就能继续干活。
                off = [n for n in targets if not self.pool.handles[n].enabled]
                for n in off:
                    self.pool.set_enabled(n, True)
                for n in targets:
                    if n not in off:
                        self.pool.handles[n].connect(force=True)
                self.tools, self._routes = self._collect_tools()
                rows = self.pool.status() if not target else [
                    s for s in self.pool.status() if s["name"] == target]
                text = "重连完成：\n" + "\n".join(
                    f"[{s['state']}] {s['name']}  tools={s['toolCount']}"
                    + (f"  错误：{s['error']}" if s["error"] else "") for s in rows)
                if off:
                    perr = self._persist_enabled(off)
                    text += (f"\n其中 {', '.join(off)} 原本是「未启用」，已自动开启"
                             + ("（已写回配置文件，重启后仍是开启）" if not perr
                                else f"（写回配置失败：{perr}，本次仅在内存生效）"))
                text += f"\n当前可用工具合计 {len(self.tools)} 个。"
                bad = [s["name"] for s in rows if s["state"] in ("error", "degraded")]
                if bad:
                    text += (f"\n{', '.join(bad)} 仍然连不上——这多半是对应软件没启动"
                             f"（超级取证大师 / IDA），先把软件打开再重连；"
                             f"在此之前改用别的服务器或技能 CLI。")
                if not quiet:
                    print(f"  ← 重连 {target or '全部'}：{text.splitlines()[1] if len(text.splitlines())>1 else ''}")
        else:
            text = f"【未知内置工具】{name}"

        elapsed = time.time() - t0
        emit({"type": "tool_end", "name": name, "server": "__sys__",
              "elapsed": round(elapsed, 2),
              "preview": text.replace("\n", " ")[:200], "full": text[:8000],
              "error": text.startswith("【")})
        # 技能正文是操作手册，砍到 6000 字等于让模型读「半本说明书」，
        # 它会照着残缺的示例瞎猜命令——这也是「同一命令反复失败」的来源之一。
        cap = (self.skill_max_chars
               if name in ("sys_skill_read", "sys_skill_list") else self.max_tool_chars)
        if len(text) > cap:
            text = text[:cap] + f"\n…（已截断，原长 {len(text)} 字符）"
        return text

    # ---------------- 配置写回 ----------------
    def _persist_enabled(self, names: list[str]) -> str | None:
        """把「已启用」写回配置文件，成功返回 None，失败返回错误文本。

        只改 enabled 这一位，其余字段原样保留（避免把用户在界面上填的
        command / 环境变量之类写丢）。写失败不算致命——内存里已经连上了，
        本轮照样能用，只是重启后会回到未启用。
        """
        try:
            import settings_store as store
            disk = store.load(self.cfg_path)
            servers = disk.setdefault("mcpServers", {})
            for n in names:
                servers.setdefault(n, {})["enabled"] = True
            store.save(self.cfg_path, disk)
            for n in names:
                self.cfg.setdefault("mcpServers", {}).setdefault(n, {})["enabled"] = True
            return None
        except Exception as e:                     # noqa: BLE001 配置写坏不该拖垮对话
            return f"{type(e).__name__}: {e}"

    # ---------------- 健康检查 ----------------
    def health_check(self) -> dict:
        """逐服务器探活。对有 health_check_dbus 的服务器深探，其余以 tools/list 成功为准。"""
        t0 = time.time()
        rows, detail = [], None
        for h in self.pool.handles.values():
            row = {"name": h.name, "label": h.label, "state": h.state,
                   "ready": h.ready, "error": h.error, "toolCount": len(h.tools),
                   "probe": None, "detail": None}
            if h.ready:
                health_tool = next(
                    (t["name"] for t in h.tools if "health_check" in (t.get("name") or "")), None)
                if health_tool:
                    try:
                        res = h.client.call_tool(health_tool, {}, timeout=60)
                        sc = res.get("structuredContent")
                        row["probe"] = health_tool
                        row["detail"] = sc
                        row["ready"] = bool((sc or {}).get("ready", True))
                        if not row["ready"]:
                            row["state"] = "degraded"
                        if detail is None:
                            detail = sc
                    except McpError as e:
                        row["error"] = f"探针失败：{e}"
                        row["state"] = "degraded"
                else:
                    row["probe"] = "tools/list"
            rows.append(row)

        ready = [r for r in rows if r["ready"] and r["state"] != "disabled"]
        self.session.log("health", elapsed=round(time.time() - t0, 2),
                         rows=[{k: v for k, v in r.items() if k != "detail"} for r in rows])
        text = "\n".join(
            f"[{'OK' if r['ready'] and r['state'] != 'disabled' else ('OFF' if r['state']=='disabled' else 'FAIL')}] "
            f"{r['name']}  tools={r['toolCount']}" + (f"  错误：{r['error']}" if r["error"] else "")
            for r in rows)
        return {"text": text, "detail": detail, "servers": rows,
                "ok": bool(ready) or all(r["state"] == "disabled" for r in rows)}

    # ---------------- 主循环 ----------------
    def new_history(self):
        return [{"role": "system", "content": self._system_prompt()}]

    def run(self, prompt: str, max_turns: int = 40, history=None, on_event=None,
            tool_budget: int = 120, renew_budget: int = 3, control=None):
        """跑一轮问答。history 传列表可保留多轮上下文；返回 (答复, history)。

        on_event(dict) 会收到 turn / think / tool_start / tool_end / tool_blocked /
        renew / inject / stopped / final 事件。

        control 传一个 RunControl，就能在被调用期间**中断**或**插话**：
          - 中断：下一个检查点收手，不再调模型收口（用户主动停的，不需要
            模型再补一段总结，那反而又要等十几秒）
          - 插话：下一轮开头作为 user 消息注入，模型据此改方向

        两个预算，分开算：
          max_turns   —— 模型「想几次」的上限，防死循环
          tool_budget —— 实际工具调用次数，代表真实工作量

        任一用尽时**不立刻掐断**，先看还在不在推进：最近一轮既没报错、也没重复参数、
        也没吐回重复结果 → 自动续一批额度（最多 renew_budget 批）。
        真正该停的信号交给早停检测，轮数只是最后的保险丝。
        这样「跑 20 轮就断」不会误伤一个正在稳步推进的长任务。
        """
        def emit(ev):
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

        def halted() -> bool:
            return bool(control and control.stopped())

        def fuse(tool_name: str, why: str, cooldown: int = 2):
            """熔断一个工具（带冷却）。返回是否已彻底停用。

            半开的理由很实际：`sqlite_query` 一被摘掉，模型就没有任何读数据的手段了，
            接下来的"结论"只能是编。所以关它 2 轮再放行一次，让它换参数重试；
            连续熔断 3 次（说明真用不了）才彻底摘除。
            """
            info = disabled.get(tool_name) or {"why": why, "until": 0, "times": 0}
            info["why"] = why
            info["times"] += 1
            info["until"] = turns_used + cooldown
            disabled[tool_name] = info
            return info["times"] >= 3

        max_turns = max(1, int(max_turns or 40))
        tool_budget = max(1, int(tool_budget or 120))
        try:
            renew_budget = max(0, min(10, int(renew_budget)))
        except (TypeError, ValueError):
            renew_budget = 3

        messages = history if history is not None else self.new_history()

        # ---- 同一问题被反复追问时，先摸清「上一轮到底答了什么」 ----
        # 实测事故最危险的一幕：前两轮老实答「查不到」，第三次追问模型直接给了
        # 题目示例里的 `123456`，还编了一整条 SAM→bootkey→NTLM→字典命中的证据链。
        # 这不是知识问题，是**迎合**：被追问三次就改口，是 LLM 的固有倾向，
        # 提示词压不住，只能由代码在收口前拦一道。
        norm_q = re.sub(r"[\s\W_]+", "", prompt or "")[:160]
        asked_before = sum(
            1 for m in messages
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("【")
            and re.sub(r"[\s\W_]+", "", str(m.get("content", "")))[:160] == norm_q)
        prev_answer = ""
        for m in reversed(messages):
            if m.get("role") == "assistant" and not m.get("tool_calls"):
                prev_answer = str(m.get("content") or "")
                break
        prev_was_hedged = bool(prev_answer) and bool(HEDGE.search(prev_answer)) \
            and not HARD_CLAIM.search(prev_answer)

        # 示例值脱敏后再送模型：题目里的「如 XXX」是格式演示，落盘仍保留原文
        sent_prompt = scrub_examples(prompt)
        # 这一问问的是「你」还是「案子」：元问题（你能干什么 / 怎么用）不进取证闸门。
        # 它本来就没有检材、没有工具、没有证据可引，硬套证据门禁只会逼出一张白卷。
        meta_turn = is_meta_ask(prompt)
        # 历史同类问题：把「上次怎么解的」附在问题后面，省一轮摸索。
        # 元问题（问你能干什么）不掺这个——那是自我介绍，不需要翻旧账。
        if not meta_turn and self._learn_on():
            try:
                _ref = learn.recall_text(BASE_DIR, prompt)
                if _ref:
                    sent_prompt = sent_prompt + _ref
                    if self.verbose:
                        print("\n[参考] 命中历史上问过的同类问题，已附解法提示")
            except Exception:
                pass
        messages.append({"role": "user", "content": sent_prompt})
        self.session.log("user", text=prompt)
        self._trim(messages)
        # 挂到实例上：_call_builtin 里的长命令要靠它判断「该不该提前杀」
        self._control = control

        # 工具表对齐到「此刻」的连接池：用户在界面上拨了开关、或服务器刚重连成功，
        # 都不能再拿构造时的旧快照去回答（那会变成「池里 49 个却说自己 0 个工具」）。
        self.refresh_tools()

        llm = self._llm()
        tools = self.tool_schemas()

        seen: dict[str, dict] = {}    # 工具+参数指纹 -> {"n": 命中次数, "out": 上次结果}
        fails = 0                     # 连续「执行失败」次数（当前这个工具）
        blocked_streak = 0            # 连续「被安全闸拒绝」次数（当前这个工具）
        # 按工具分别计数：A 失败一次不该把 B 的账算到自己头上。
        # 熔断按「单个工具连续 3 次」判定，换工具就重新开始数。
        fail_count: dict[str, int] = {}
        block_count: dict[str, int] = {}
        # 被熔断的工具：名字 -> {"why": 原因, "until": 冷却到第几轮, "times": 熔断过几次}。
        # 关键在**半开**：以前一熔断就整轮摘掉，模型于是失去唯一能查数据的工具，
        # 只能靠"推理"编答案（实测它就编了一条 123456 的证据链）。
        # 现在熔断只关 2 轮，到点自动放行一次；连续熔断 3 次的工具才彻底摘除。
        disabled: dict[str, dict] = {}
        fused_calls = 0               # 熔断后仍被点名调用的次数（兜底防死循环）
        force_text = False            # 模型死盯熔断工具时，收回工具让它只能写结论
        last_err = ""                 # 最近一条失败/拒绝的首行，收口时要原样给出来
        last_sig = ""                 # 上一次工具结果的指纹
        last_sig_tool = ""            # 该指纹属于哪个工具
        same_streak = 0               # 同一工具连续多少次返回一样的内容
        stop = ""                     # 提前停止的原因
        selfcheck_done = False        # 「说没工具但其实有」只纠正一次
        ok_calls = 0                  # 本轮**成功**返回的工具调用次数（证据基数）
        gate_used = 0                 # 证据门禁打回过几次（最多 2 次，不能卡死）
        evidence_text: list[str] = [] # 本轮所有工具返回的原文，用于核对"引用是否真存在"
        # ---- 「撞墙就交白卷」检测 ----
        # blockers: 本轮撞到的阻塞型报错（工具名 + 关键词），例如 file_too_large。
        # routes_done: 已经换过的通道（reconnect / raw / copy / local / mount / index）。
        # surrender_used: 已因「没换够路就写未取得」打回过几次。
        blockers: list[tuple[str, str]] = []
        routes_done: set[str] = set()
        surrender_used = 0
        ask_used = 0                  # 已因「结尾征求许可」打回过几次
        meta_used = 0                 # 已因「元问题却交白卷」打回过几次
        # ---- 经验沉淀用的账本（learn.py 消费）----
        gate_hint = ""                # 第一次被证据闸拦下的那句断言
        injects: list[str] = []       # 用户在跑的过程中插的话（多为纠正）
        used_tools: list[str] = []    # 真正成功返回过的工具名

        turns_used = 0                # 已用轮数
        calls_used = 0                # 已用工具调用次数
        turn_cap = max_turns          # 当前轮数上限（续期会涨）
        call_cap = tool_budget        # 当前工具调用上限（续期会涨）
        renews = 0                    # 已续期次数

        def learn_now(ans: str):
            """本轮结束前把经验落盘。抽成小函数是为了三处 return 都能调到。"""
            self._learn_round(prompt, ans, {
                "gates": gate_used, "gate_hint": gate_hint,
                "surrender": surrender_used,
                "blocker": (f"{blockers[-1][0]} {blockers[-1][1]}" if blockers else ""),
                "selfcheck": 1 if selfcheck_done else 0,
                "fused": list(disabled.keys()),
                "blockers": list(blockers[-2:]),
                "corrects": list(injects),
                "ok_calls": ok_calls,
                "tools": list(used_tools),
            })

        while turns_used < turn_cap:
            # ---- 检查点 1：用户点了中断 ----
            if halted():
                stop = "用户手动中断"
                break

            # ---- 检查点 2：用户插话 ----
            # 放在轮首而不是工具循环里：此刻上一条 assistant(tool_calls) 的
            # tool 响应已经全部补齐，插一条 user 消息不会破坏消息序列。
            if control:
                for extra in control.drain():
                    injects.append(extra)
                    messages.append({
                        "role": "user",
                        "content": ("【用户插话·请据此调整方向，不要忽略】\n" + extra),
                    })
                    self.session.log("inject", text=extra, turn=turns_used)
                    emit({"type": "inject", "text": extra, "turn": turns_used})
                    if self.verbose:
                        print(f"\n[插话] {extra[:120]}")

            turns_used += 1
            emit({"type": "turn", "n": turns_used, "max": turn_cap,
                  "calls": calls_used, "callMax": call_cap, "renews": renews})
            if self.verbose:
                print(f"\n[第 {turns_used}/{turn_cap} 轮 · 工具 {calls_used}/{call_cap}] 思考中…")
            # 熔断掉的工具暂时不下发给模型：它看不见，就不会去点。
            # 「暂时」是关键——冷却期（默认 2 轮）过后自动放行一次，
            # 否则唯一能查数据的工具一挂，模型就只能编。
            active_tools = tools
            if disabled:
                cooling = {n for n, d in disabled.items()
                           if d["times"] >= 3 or turns_used <= d["until"]}
                active_tools = [t for t in tools
                                if (t.get("function") or {}).get("name") not in cooling]
            if force_text:
                active_tools = None      # 模型死活要那条路 → 收回工具，逼它先给结论
            try:
                msg = llm.chat(messages, active_tools or None)
            except Exception as e:
                # 模型接口挂了（500 / 401 / 超时）不能把整轮对话炸掉：
                # 炸掉的话这一问就只剩「提问」没有「回答」，历史里看着像答案丢了。
                # 记下原因，走下面的收口逻辑，至少把已经查到的和为什么停讲清楚。
                err = f"{type(e).__name__}: {str(e)[:200]}"
                stop = f"模型接口出错，没能继续推理（{err}）"
                if self.verbose:
                    print(f"\n[模型接口出错] {err}")
                break
            tool_calls = msg.get("tool_calls") or []
            # 有的模型（尤其 DeepSeek 系走中转时）会把原生工具调用标记当正文吐出来，
            # 这类内容既不能进报告，也不该当成「已经查过了」。清掉，并留个说明。
            raw_content = msg.get("content") or ""
            content_clean = strip_tool_markup(raw_content)

            assistant_msg = {"role": "assistant", "content": content_clean}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                content = content_clean
                if not content.strip() and raw_content.strip():
                    content = ("（这一轮模型返回的是工具调用标记的纯文本，没能解析成实际调用，"
                               "已丢弃。可以再问一次，或者把任务拆小一点。）")
                elif not content.strip() and (disabled or force_text):
                    # 工具被熔断/收回后模型偶尔会吐空正文。历史里不能留一条空回答。
                    content = ("（可用工具都已被停用，模型这一轮也没给出文字。"
                               "上面几轮的工具卡片里有已经查到的内容；"
                               "把需要的那一步告诉我，我换个办法再来。）")
                # ---- 自校验：一次工具都没调，却说「没有可用工具」 ----
                # 这是最坑的一种错：它把「我没查」说成「环境不允许」，用户只能干瞪眼。
                # 只纠正一次（避免死循环），纠正后再犯就照原样给出去。
                if (not selfcheck_done and self.tools and calls_used == 0
                        and NO_TOOL_CLAIM.search(content or "")):
                    selfcheck_done = True
                    live_names = [s["name"] for s in self.pool.status() if s["state"] == "ready"]
                    emit({"type": "selfcheck",
                          "text": f"模型声称没有工具，实际有 {len(self.tools)} 个（"
                                  f"{'、'.join(live_names) or '—'}），已要求它核实后重做"})
                    messages.append({"role": "user", "content": (
                        "【系统校验·必须执行】你上一条答复在说「没有可用工具 / 服务器被禁用」，"
                        f"但当前实际有 {len(self.tools)} 个工具，"
                        f"{'、'.join(live_names) or '服务端'} 处于 ready。"
                        "现在立刻调 `sys_server_status` 核实：返回 ready 就接着把问题做完，"
                        "不许再用「工具不可用」当结论，也不许让用户去改配置文件。")})
                    continue
                # ---- 元问题白卷闸：问「你能干什么」却答「未取得」 ----
                # 这一条专治用户截图里的那幕：问能力，回一张「结论：未取得」的表，
                # 下面还写「关于「密码」一词」逐句自证清白。用户在问你是谁，
                # 不是在让你查案——交白卷比不答更让人火大。
                if (meta_turn and meta_used < 1
                        and (GIVEUP_RE.search(content or "")
                             or SELF_JUSTIFY.search(content or ""))
                        and not CAPABILITY_RE.search(content or "")):
                    meta_used += 1
                    emit({"type": "gate",
                          "text": "用户在问你能干什么，不该答「未取得」，已要求直接给能力清单"})
                    messages.append({"role": "user", "content": (
                        "【元问题重置·必须执行】用户这一问是在问**你能干什么 / 怎么用**，"
                        "不是在让你查某个检材。所以：\n"
                        "① 不要写「未取得」「无法」「查不到」，这里根本没有要找的东西；\n"
                        "② 不要解释上一条答复、不要引用「密码」一词做自我辩护，"
                        "用户没在质问你；\n"
                        "③ 直接用清单回答：能读什么（本机文件 / 火眼挂载视图 / 镜像）、"
                        "用什么读（内置直读工具、技能 CLI、可选 MCP）、能答哪几类题"
                        "（进程与账号、浏览器与聊天记录、加密容器、时间线……），"
                        "最后一行给一句「把路径或案件号发我就能开工」。\n"
                        "现在重写这一条答复。")})
                    continue

                # ---- 证据门禁：没有工具支撑的「硬断言」，不许当结论交出去 ----
                # 两道，都是实测踩出来的：
                #  ① 一次工具都没成功，却给出「密码是 123456 / 哈希命中 / 卷标为 X」
                #     —— 编造的不是结论，是整条证据链（SAM→bootkey→NTLM→字典）。
                #  ② 上一轮老实说「查不到」，用户追问三次后这次改口给了具体值
                #     —— 这是迎合，不是新证据。
                # 只打回 2 次，避免把用户永久卡住；打回后仍不改口就照实交付，
                # 但会在答复里保留"未能复核"的痕迹。
                gate = ""
                claim = ""
                if not meta_turn and gate_used < 2 and is_hard_claim(content or "") \
                        and not HEDGE.search(content or ""):
                    _hm = HARD_CLAIM.search(content)
                    claim = _hm.group(0) if _hm else ""
                    # 光记「密码」两个字没信息量，把被拦的那个值也带上，
                    # 这条教训下次才看得懂（也方便用户判断拦得对不对）
                    for _sv in SPECIFIC_VALUE.finditer(content or ""):
                        _v = _sv.group(0).strip("`「」\"'")
                        if _v and not PATHISH.match(_v):
                            claim = f"{claim} {_v}"
                            break
                    if ok_calls == 0:
                        gate = (f"你这条答复里出现了硬断言（「{claim}」），但本轮**没有任何一次**"
                                f"工具调用成功返回。没有工具结果就不能给具体值。\n"
                                f"现在二选一：① 立刻用能用的工具去查（技能 CLI / sqlite_query / "
                                f"MCP 工具），拿到原文再写；② 把结论改成「未取得」，并说明"
                                f"缺哪个工具、缺哪份数据。\n"
                                f"**禁止**编造证据链（例如「SAM 取 bootkey → NTLM → 字典命中」），"
                                f"你没跑过就是没跑过。")
                    elif prev_was_hedged and asked_before >= 1:
                        gate = (f"同一个问题你已经答过「未取得/查不到」，这次改口给了具体值"
                                f"（「{claim}」）。重复追问**不是新证据**，用户催得急不等于答案变了。\n"
                                f"如果本轮没有任何新的工具成功返回，就维持上一轮的结论并说明"
                                f"「仍然没有查到」；确实拿到了新结果，就把**返回原文**贴出来再改口。")
                    elif gate_used < 1 and evidence_text:
                        # 核对引用：答案里引号包着的具体值，必须能在本轮工具原文里找到。
                        # 这一条专治「我跑过了 / 返回里就是这么写的」这种口头证据。
                        bad = unsupported_values(content or "", "\n".join(evidence_text))
                        if bad:
                            gate = (f"你引用的值 {('、'.join('`' + v + '`' for v in bad[:3]))} "
                                    f"在本轮**任何工具返回里都找不到**。\n"
                                    f"要么把它换成工具原文里真实出现的值，要么删掉这个结论、"
                                    f"改成「未取得」。不许以「根据经验 / 推测」为由补一个像样的值。")
                if gate:
                    gate_used += 1
                    if not gate_hint:
                        gate_hint = claim
                    emit({"type": "gate", "text": f"拦住一条无证据支撑的结论（{claim}），"
                                                  f"已要求重写"})
                    messages.append({"role": "user", "content": f"【证据门禁·必须执行】{gate}"})
                    continue

                # ---- 第六道闸：撞了墙就写「未取得」，但换路清单一条都没走 ----
                # 这一条专治「模范式交白卷」：格式漂亮、每条都附了原文、还写了下一步建议，
                # 可它只是把「换个通道再查一次」写成了**建议**，自己一步没走。
                # 取证里这比编答案更隐蔽——它看起来完全合规，实际把证据弄丢了。
                if not meta_turn and not gate and surrender_used < 2 and blockers \
                        and GIVEUP_RE.search(content or "") \
                        and not HUMAN_BLOCK_RE.search(content or "") \
                        and (self.tools or len(self.skills.skills)):
                    alt_done = routes_done & {"raw", "copy", "local", "mount", "index"}
                    if "reconnect" not in routes_done or not alt_done:
                        surrender_used += 1
                        btool, bword = blockers[-1]
                        todo = []
                        if "reconnect" not in routes_done:
                            todo.append(
                                "① **先把后台接回来**：`sys_server_reconnect` → "
                                "`ges case open <案件号>` → `ges status`。"
                                "HTTPError / Read timed out / case_backend_unreachable "
                                "都是「服务掉了」，不是「数据不存在」。")
                        if not alt_done:
                            todo.append(
                                "② **换一条真正能读大文件的通道**（下面任一条，别只写进建议）：\n"
                                "   · `raw_data_content_search` / `global_search_content`："
                                "裸数据 / 全文检索，`maxFileSize=-1` **不受单文件大小限制**，"
                                "专治 `file_too_large`；返回 taskId 后 `query_task_status` 看 "
                                "`taskResultCount`；\n"
                                "   · `load_case`（caseId + dbPath）→ `query_device_info` 拿 "
                                "`mount_path` / `dokan_path` → 直接 `sys_file_scan` 扫挂载视图里的 "
                                "大文件（不用复制，18 GB 也能流式扫）；\n"
                                "   · 没有挂载视图时：`ges copy <虚拟路径> --max-total-bytes 20000000000` "
                                "复制到本地，再 `sys_file_scan`；\n"
                                "   · 把 .vhdx / .E01 当**独立检材**：`add_image_material` → "
                                "`submit_computer_forensic_task(materialKind=image)` → "
                                "`wait_task_completion` → `load_case`，再用 dokan 挂载读；\n"
                                "   · 索引类问题（not_created / HTTPError）→ `create_file_index_task` "
                                "或 `ges index build --wait`；建不起来就退回上面几条，"
                                "**0 命中不能当成「不存在」**。")
                        gate_text = (
                            f"你在 `{btool}` 上撞到的是「{bword}」——这类报错只说明"
                            f"**这条通道走不通**，不说明东西不在。可你直接写了「未取得」，"
                            f"换路清单一条都没试（已走通道："
                            f"{'、'.join(ROUTE_LABEL[r] for r in sorted(routes_done)) or '无'}）。\n\n"
                            + "\n".join(todo) +
                            "\n\n现在按顺序做，能同时发的就在一轮里一起发。"
                            "只有这些通道都试完（或确实需要人工介入：软件没装、要授权、要密码）"
                            "才允许写「未取得」，并在结论里写清**每条通道分别报了什么错**。"
                            "把「下一步建议」留给你自己做不了的事，能查的自己查。")
                        emit({"type": "gate",
                              "text": f"结论只说「未取得」，但换路清单没走（撞到 {bword}），已要求继续查"})
                        messages.append({"role": "user",
                                         "content": f"【换路门禁·必须执行】{gate_text}"})
                        continue

                # ---- 第七道闸：别把决定权推回给用户 ----
                # 实测最常见的「假交付」就是结尾一句「要我现在就…吗？」——
                # 看起来礼貌，实际是把该自己干的活退回给用户，然后这一问就废了。
                # 只在「还有轮数、还有工具、确实是自己能做的事」时打回。
                if (not meta_turn and ask_used < 2 and (self.tools or len(self.skills.skills))
                        and turns_used < turn_cap and calls_used < call_cap
                        and ASK_PERMISSION.search((content or "").strip()[-400:])):
                    ask_used += 1
                    tail = (content or "").strip().splitlines()[-1][:120]
                    emit({"type": "gate", "text": "结尾在征求许可，已要求它自己继续干"})
                    messages.append({"role": "user", "content": (
                        "【不许把决定权推回去】你的结尾是「" + tail + "」——这是在问用户要不要继续。\n"
                        "用户的诉求是**答案**，不是选择题。凡是你自己能用工具做的，"
                        "现在就做完再收口，别再问「要不要」「需要我吗」。\n"
                        "只有这四类才允许问用户：软件没装/没启动、需要授权或密码、"
                        "需要用户提供镜像外的文件、需要用户做取证之外的决定。\n"
                        "现在就按你自己列的那条路继续往下查，用工具把结果拿出来。")})
                    continue

                self.session.log("assistant_final", text=content)
                emit({"type": "final", "text": content})
                self._control = None
                learn_now(content)
                return content, messages

            if content_clean:
                emit({"type": "think", "text": content_clean})
                if self.verbose:
                    print("  · " + content_clean.strip().replace("\n", " ")[:200])

            stalled = False           # 本轮是否已出现「白跑」信号：报错 / 重复参数 / 重复结果

            for idx, tc in enumerate(tool_calls):
                # ---- 检查点 3：中断可能发生在两个工具之间 ----
                # 这条 assistant 声明了 N 个 tool_calls，只回 M 个会让下一轮
                # 请求 400。剩余的全部补一条「已中断」的 tool 响应。
                if halted():
                    stop = "用户手动中断"
                    for rest in tool_calls[idx:]:
                        messages.append({"role": "tool", "tool_call_id": rest.get("id"),
                                         "content": "【已中断】用户手动停止了本次推理，"
                                                    "这一调用未执行。"})
                    break

                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = None

                # ---- 已熔断的工具：直接回绝，不执行，也不算新失败 ----
                # 正常情况它已经被从工具清单里摘掉了，模型看不见就不会点；
                # 这里只是兜底（有的模型会照着历史里的旧名字硬调）。
                # 冷却期已过的放行——只拦「刚熔断、还没冷却」和「彻底停用」的。
                if name in disabled and (disabled[name]["times"] >= 3
                                         or turns_used <= disabled[name]["until"]):
                    fused_calls += 1
                    out = FUSED_REPLY.format(name=name, why=disabled[name]["why"])
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
                    emit({"type": "tool_blocked", "name": name, "args": args or {},
                          "reason": "该工具已熔断"})
                    stalled = True
                    if fused_calls >= 3 and not force_text:
                        # 模型盯着熔断工具不放 → 不再跟它耗，直接收回工具清单，
                        # 下一轮它只能写字，自然会给出结论，而不是停在一条报错上。
                        force_text = True
                    elif fused_calls >= 8:
                        stop = (f"模型连续 {fused_calls} 次调用已被熔断的工具（最近一次 {name}）")
                        for rest in tool_calls[idx + 1:]:
                            messages.append({"role": "tool", "tool_call_id": rest.get("id"),
                                             "content": "【已跳过】本轮已提前停止。"})
                        break
                    continue

                # ---- 指纹：工具名 + 规范化参数。完全相同的调用不该再花额度 ----
                fp = ""
                if args is not None:
                    try:
                        fp = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
                    except Exception:
                        fp = name + "|" + str(args)

                hit = seen.get(fp) if fp else None
                # 轮询类工具（wait_* / *status / progress / job）**本来就该重复调**——
                # 解析进度、任务状态就是这么查的。对它们只做「结果没变」判定，不做重复短路。
                # `ges task ls` / `ges job status` 走的也是 sys_skill_exec，所以命令串也要看。
                poll = bool(POLL_HINTS.search(name)) or (
                    name == "sys_skill_exec"
                    and bool(POLL_HINTS.search(str((args or {}).get("command") or ""))))

                # ---- 第 3 次相同调用：不再整轮停下，而是熔断这一个工具 ----
                # 原来这里直接 `stop`，用户看到的是一句「本次在…处停下」，任务也没做完。
                # 改成：把这个工具从清单里摘掉，让模型必须换路子，本次任务继续往下走。
                if hit and not poll and hit["n"] >= 2:
                    hit["n"] += 1
                    prev = str(hit.get("out") or "")
                    pk = failure_kind(prev)
                    if pk == "blocked":
                        why = f"同样的参数连续 {hit['n']} 次被安全闸拒绝"
                    elif pk == "failed":
                        why = f"同样的参数连续 {hit['n']} 次执行失败"
                    else:
                        why = f"用完全相同的参数调了 {hit['n']} 次，返回内容不变"
                    fuse(name, why)
                    out = FUSED_REPLY.format(name=name, why=why) + FUSE_NOTE
                    emit({"type": "tool_start", "name": name, "server": "__sys__", "args": args})
                    emit({"type": "tool_end", "name": name, "server": "__sys__", "elapsed": 0,
                          "preview": f"{name} 已熔断（{why}）", "full": out, "error": True})
                    emit({"type": "tool_fused", "name": name, "why": why})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
                    stalled = True
                    continue

                # ---- 第 2 次相同调用：不真跑，回缓存 + 明确要求换路子 ----
                if hit and not poll:
                    hit["n"] += 1
                    prev = str(hit.get("out") or "")
                    out = ("【重复调用·已跳过】这条调用（" + name + "，同样的参数）刚刚才跑过，"
                           "结果不会变，所以这次没有再执行。上一次的返回如下：\n"
                           + prev[:1200] + ("…" if len(prev) > 1200 else "") + "\n\n"
                           "→ 不要再调它了。要么换参数、换工具，要么直接用上面这些信息往下推进/给结论。")
                    emit({"type": "tool_start", "name": name, "server": "__sys__", "args": args})
                    emit({"type": "tool_end", "name": name, "server": "__sys__", "elapsed": 0,
                          "preview": "重复调用已跳过（结果与上次相同）", "full": out, "error": True})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
                    stalled = True
                    continue

                # ---- 额度用尽：剩下的这一轮不跑了，但缺 tool 响应会让接口 400 ----
                if calls_used >= call_cap:
                    for rest in tool_calls[idx:]:
                        messages.append({"role": "tool", "tool_call_id": rest.get("id"),
                                         "content": "【本轮额度已用尽，这一调用被跳过；"
                                                    "若自动续期成功，下一步会重新安排】"})
                    break

                if args is None:
                    calls_used += 1
                    out = f"【参数解析失败】模型给出的 arguments 不是合法 JSON：{str(raw_args)[:300]}"
                    emit({"type": "tool_blocked", "name": name, "args": {}, "reason": "参数不是合法 JSON"})
                    fail_count[name] = fail_count.get(name, 0) + 1
                    fails = fail_count[name]
                    if fails >= 3:
                        fuse(name, f"连续 {fails} 次给出非法 JSON 参数")
                        out += FUSE_NOTE
                        emit({"type": "tool_fused", "name": name,
                              "why": f"连续 {fails} 次参数不是合法 JSON"})
                    messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})
                    stalled = True
                    continue

                calls_used += 1
                try:
                    out = self.call(name, args, on_event=emit)
                except Exception as e:
                    # 模型编了个不存在的工具名，或参数跟 schema 完全对不上。
                    # 这种错不能把整轮对话带崩——当成一条工具结果喂回去让它自己改，
                    # ReAct 本来就该靠 tool 消息自我纠错。
                    out = f"【工具异常】{type(e).__name__}: {e}"
                    emit({"type": "tool_start", "name": name, "server": "", "args": args})
                    emit({"type": "tool_end", "name": name, "server": "", "elapsed": 0,
                          "preview": out, "full": out, "error": True})
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": out})

                # ---- 记账：这条调用走了哪条换路通道、有没有撞上阻塞型报错 ----
                # 收口时「未取得」合不合法，就看这两个集合——不听模型自己说，
                # 看它到底调了什么。sys_file_scan 即使 0 命中也算走了通道（那是真扫过了）。
                #
                # 注意：必须是**报错**才算撞墙。`index_state=current` 这种正常字段里也带
                # 关键词，按「出现即算」会把一次成功的检索记成一次失败，门禁就变成噪音了。
                routes_done |= routes_from_call(name, args)
                looks_bad = bool(failure_kind(out)) or bool(re.search(
                    r'(?i)"code"\s*:\s*"[a-z_]{3,}"|\bERROR\b|error:|失败|超时|拒绝|不可用',
                    (out or "")[:800]))
                if looks_bad:
                    bm = BLOCKER_RE.search(out or "")
                    if bm:
                        word = bm.group(0)[:60]
                        if word.lower() not in {w.lower() for _, w in blockers}:
                            blockers.append((name, word))

                # ---- 进展检测 ----
                if fp:
                    seen[fp] = {"n": 1, "out": out}
                # 策略拒绝（安全闸）和执行失败分开数：前者的解法是「换命令/换工具」，
                # 后者才可能是连接或环境问题。混在一起会给出错误的停止原因。
                fkind = failure_kind(out)
                if fkind == "blocked":
                    block_count[name] = block_count.get(name, 0) + 1
                    fail_count[name] = 0
                    blocked_streak = block_count[name]
                    fails = 0
                elif fkind == "failed":
                    fail_count[name] = fail_count.get(name, 0) + 1
                    block_count[name] = 0
                    fails = fail_count[name]
                    blocked_streak = 0
                else:
                    fails = 0
                    blocked_streak = 0
                    fail_count[name] = 0
                    block_count[name] = 0
                    # 成功返回 = 一条可引用的证据。「模型有没有资格下硬断言」就看这个数。
                    # 读技能/看服务器状态不算：那是说明书和仪表盘，不含案件数据。
                    if out and out.strip():
                        evidence_text.append(out)
                        if name not in INTROSPECTION_TOOLS:
                            ok_calls += 1
                            if name not in used_tools:
                                used_tools.append(name)
                if fkind:
                    stalled = True
                    last_err = out.splitlines()[0][:160] if out.splitlines() else out[:160]
                    # 第 2 次就把「换条路」写进 tool 结果里。等到第 3 次才停太晚了：
                    # 模型手里一直没有任何「别这么干」的信号，只会换写法继续撞。
                    if fkind == "blocked" and blocked_streak == 2:
                        out += STEER_BLOCKED
                        messages[-1]["content"] = out
                    elif fkind == "failed" and fails == 2:
                        out += STEER_FAILED
                        messages[-1]["content"] = out

                    # ---- 第 3 次：熔断这个工具，但**不停整轮任务** ----
                    # 用户明确不要「连续 3 次报错就收工」。真连不上的是这一个工具，
                    # 换个工具/换个路子照样能把案子往下做。所以这里只摘它，
                    # 停不停让「有没有路可走」决定，而不是让失败计数决定。
                    streak = blocked_streak if fkind == "blocked" else fails
                    if streak >= 3:
                        why = (f"连续 {streak} 次被安全闸拒绝" if fkind == "blocked"
                               else f"连续 {streak} 次执行失败")
                        fully = fuse(name, why)
                        out += FUSE_NOTE
                        messages[-1]["content"] = out
                        emit({"type": "tool_fused", "name": name, "why": why,
                              "left": max(0, len([t for t in tools
                                                  if (t.get("function") or {}).get("name")
                                                  not in disabled])),
                              "final": fully})
                        if self.verbose:
                            print(f"\n[熔断] {name}（{why}），本次任务内不再下发这个工具")
                        fails = 0
                        blocked_streak = 0

                # 结果指纹：参数可以微调，但连续吐回一模一样的内容就是真的没进展。
                # 比「参数相同」更狠——模型换个参数接着问，答案还是那一份，一样是空转。
                # 这里也**只熔断单个工具**：换一个工具回来同样的东西，那是另一件事，
                # 不该把账记到别人头上（所以指纹要连工具名一起比）。
                rh = hashlib.md5(out.strip().encode("utf-8", "replace")).hexdigest()[:12]
                if rh == last_sig and name == last_sig_tool:
                    same_streak += 1
                    stalled = True
                else:
                    last_sig, last_sig_tool, same_streak = rh, name, 1

                # 轮询类工具「结果没变」是**正常状态**——任务在跑就是没变。
                # 以前这里照样在 5 次后熔断，于是模型失去了唯一的等任务手段，
                # 只能交白卷（实测：query_task_status 被停用后整题报废）。
                # 现在轮询类放宽到 12 次，中期只催它换姿势（阻塞等待/先去查别的），
                # 真·空转 12 次才熔断。
                poll_limit = 12
                if poll and same_streak in (2, 3, 5, 7) and name not in disabled:
                    out += ("\n\n【别再连续轮询了】结果和上次一模一样，说明任务还在跑。"
                            "→ 正确姿势：① 用 `wait_task_completion(taskId=…, timeoutSeconds=120)` "
                            "阻塞等待（这才是等任务的工具）；② 或者先去做别的线索，"
                            "回头再查；③ 不要再用同样的参数一秒一次地刷。")
                    messages[-1]["content"] = out
                if same_streak >= (poll_limit if poll else 3) and name not in disabled:
                    why = (f"连续 {same_streak} 次返回一模一样的内容，"
                           f"换个参数也拿不到新信息")
                    fuse(name, why)
                    out += f"\n\n【这个工具已停用】{why}。请换别的工具或换数据源；" \
                           f"确实查不出新东西就直接说明。"
                    messages[-1]["content"] = out
                    emit({"type": "tool_fused", "name": name, "why": why})
                    if self.verbose:
                        print(f"\n[熔断] {name}（{why}）")
                    same_streak = 0
                    last_sig = ""
            if stop:
                self.session.log("agent_stop", reason=stop, turn=turns_used, calls=calls_used)
                if self.verbose:
                    print(f"\n[提前停止] {stop}")
                break

            # ---- 额度结算：用完了先看还推不推进，能推进就续，别急着掐 ----
            if turns_used >= turn_cap or calls_used >= call_cap:
                if renews < renew_budget and not stalled:
                    renews += 1
                    turn_cap += max_turns
                    call_cap += tool_budget
                    emit({"type": "renew", "renews": renews, "max": turn_cap,
                          "callMax": call_cap, "turns": turns_used, "calls": calls_used})
                    self.session.log("budget_renew", renew=renews, turns=turns_used,
                                     calls=calls_used, turn_cap=turn_cap, call_cap=call_cap)
                    if self.verbose:
                        print(f"\n[额度续期 {renews}/{renew_budget}] 轮数 {turn_cap}、"
                              f"工具调用 {call_cap}，继续推进…")
                    continue
                if stalled:
                    stop = f"额度用尽，且最近一轮没有产出新信息（已跑 {turns_used} 轮、工具 {calls_used} 次）"
                elif renews:
                    stop = f"已自动续期 {renews} 次仍未完成（共 {turns_used} 轮、工具 {calls_used} 次）"
                else:
                    stop = f"共跑 {turns_used} 轮、工具 {calls_used} 次"
                self.session.log("agent_stop", reason=stop, turn=turns_used,
                                 calls=calls_used, renews=renews)
                if self.verbose:
                    print(f"\n[到达额度] {stop}")
                break

        # 跑满上限或提前停止：让它拿已收集到的信息收口，而不是丢一句冷冰冰的提示。
        # 不带 tools 再问一次，模型只能出文字，把已经查到的先讲清楚。
        note = stop or f"共跑 {turns_used} 轮、工具 {calls_used} 次"

        # 用户主动点了「中断」就不收口了：他就是要它现在停下来，
        # 再发一次请求让模型写总结，等于又要等十几秒，还多花一次 token。
        # 直接把已经跑出来的东西留在界面上，附一句说明即可。
        if control and control.interrupted:
            content = (f"（已中断 · 停在第 {turns_used} 轮、工具 {calls_used} 次。"
                       f"上面已经跑出的结果都有效，接着问就行，上下文还在。）")
            messages.append({"role": "assistant", "content": content})
            self.session.log("assistant_final", text=content, interrupted=True,
                             turn=turns_used, calls=calls_used)
            emit({"type": "stopped", "turn": turns_used, "calls": calls_used, "text": content})
            self._control = None
            learn_now(content)
            return content, messages

        tail = ""
        # 收口前再取一次插话。用户可能是在模型已经开始写总结时才点的发送——
        # 这时候已经没有「下一轮」了，不在这里接住，这句话就永远躺在队列里。
        if control:
            for extra in control.drain():
                messages.append({"role": "user",
                                 "content": ("【用户插话·请据此调整方向，不要忽略】\n" + extra)})
                self.session.log("inject", text=extra, turn=turns_used, phase="closing")
                emit({"type": "inject", "text": extra, "turn": turns_used})

        # 明确告诉它「工具已经收走了，只能用文字」。
        # 不这么说，模型会继续吐 tool_calls、content 留空，
        # 用户看到的就是「共跑 14 轮、没能生成小结」——而它明明早就有答案了。
        closing = ("【额度已用尽 · 现在必须收口】不会再给你任何工具，请只用文字回答。\n"
                   "必须包含：① 结论（能确定就直接给答案值，不要再说「需要继续查」）；\n"
                   "② 依据（哪个文件 / 哪次工具返回，贴出关键字段原文）；\n"
                   "③ 未确认项（如果没有就写「无」）。\n"
                   "你前面已经拿到的工具结果足够就立刻作答，不要重复描述过程。")
        messages.append({"role": "user", "content": closing})
        for attempt in (1, 2):
            try:
                msg = llm.chat(messages, None)
                # 收口这次是「不带工具」的请求，模型偶尔仍会把工具调用标记当正文吐出来
                # （<｜｜DSML｜｜ invoke …>）。不过滤的话，用户看到的结论就是一坨标签。
                tail = strip_tool_markup((msg.get("content") or "")).strip()
            except Exception as e:
                note += f"；收口请求失败：{type(e).__name__}: {e}"
                break
            if tail:
                break
            if attempt == 1:
                messages.append({"role": "user",
                                 "content": "（上一轮你没有输出任何文字）现在直接用文字写出结论，不要调用工具。"})
        if not tail:
            # 连收口都写不出字，至少把**真实拿到的东西**摆出来，
            # 别让用户面对一句「没能生成小结」——那等于这十几轮白跑。
            tail = digest_evidence(evidence_text)
        if tail:
            # 现在的停因只剩：额度用尽 / 熔断工具被反复点名 / 用户中断。
            # 「工具连续报错」和「结果原地踏步」都改成熔断单工具、继续干活了，
            # 所以这里不会再出现「本次在『连续 3 次工具调用都报错』处停下」那种收场。
            hint = "直接回一句「继续」就能接着往下做，上下文都还在。"
            if disabled:
                hint += (f"另外，本轮有 {len(disabled)} 个工具因反复失败被停用："
                         f"{'、'.join(disabled)}；要用它们的话，先把报错原因告诉我。")
            content = (f"{tail}\n\n---\n"
                       f"> 本次在「{note}」处暂停。上面的结论只覆盖已经查到的部分——{hint}")
        else:
            content = (f"（{note}，自动停止。没能生成小结——"
                       f"可以先看上面几轮的工具结果，或者把问题拆小一点再问。）")
        messages.append({"role": "assistant", "content": content})
        self.session.log("assistant_final", text=content)
        emit({"type": "final", "text": content})
        self._control = None
        learn_now(content)
        return content, messages

    def _trim(self, messages, limit_chars: int = 240_000, keep_pairs: int = 6):
        """历史过长时压缩中间段——但**不丢信息**。

        原来直接把中间砍掉、只留最近 3 轮。长任务一旦超过阈值，模型就「失忆」，
        把已经查过的东西整段重查一遍，正好撞上早停规则被判成「原地打转」。
        现在把砍掉的部分压成一页摘要（每次调用 + 结果前 130 字），
        模型仍然知道自己做过什么，只是不必再占那么多 token。
        """
        total = sum(len(str(m.get("content") or "")) for m in messages)
        if total <= limit_chars or len(messages) <= 5:
            return
        head = messages[:2]                       # system + 第一句用户提问
        tail = messages[-keep_pairs * 2:]         # 最近的若干条
        # 别让 tail 以 tool 消息开头：它对应的 assistant(tool_calls) 会被砍掉，
        # OpenAI 系接口对这种孤儿 tool 消息直接 400
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        # 同理，结尾也不能是「发了 tool_calls 却没有 tool 响应」的 assistant
        while tail and tail[-1].get("role") == "assistant" and tail[-1].get("tool_calls"):
            tail = tail[:-1]
        mid = messages[len(head): len(messages) - len(tail)]
        if not mid:
            return
        digest = self._digest(mid)
        messages[:] = head + ([{"role": "assistant", "content": digest}] if digest else []) + tail
        self.session.log("history_trim", dropped=len(mid), digest_chars=len(digest))

    def _digest(self, msgs, max_lines: int = 140) -> str:
        """把一段历史压成「做过什么」的清单，用来替换原文。"""
        lines = []
        for m in msgs:
            role = m.get("role")
            if role == "assistant":
                for tc in (m.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    arg = re.sub(r"\s+", " ", str(fn.get("arguments") or ""))[:90]
                    lines.append(f"· {fn.get('name')} {arg}")
                if m.get("content"):
                    lines.append("· 说明：" + re.sub(r"\s+", " ", str(m["content"]))[:90])
            elif role == "tool":
                lines.append("  → " + re.sub(r"\s+", " ", str(m.get("content") or ""))[:130])
            elif role == "user":
                lines.append("· 用户：" + re.sub(r"\s+", " ", str(m.get("content") or ""))[:90])
        if not lines:
            return ""
        if len(lines) > max_lines:
            head_n, tail_n = 20, max_lines - 20
            lines = (lines[:head_n]
                     + [f"  …（中间省略 {len(lines) - max_lines} 条记录）"]
                     + lines[-tail_n:])
        return ("【早期步骤摘要】上下文过长，以下是此前已经执行过的调用与返回（细节已省略）。"
                "同一个工具、同样的参数不要重复调用，结果不会变：\n" + "\n".join(lines))

    def _llm(self) -> LlmClient:
        c = self.cfg.get("llm") or {}
        api_key = os.environ.get("MYOS_LLM_API_KEY") or c.get("api_key") or ""
        if not api_key:
            raise LlmError(
                "没有配置模型 API Key。\n"
                "  方式一：编辑 config.json → llm.api_key\n"
                "  方式二：设置环境变量 MYOS_LLM_API_KEY\n"
                "  不想用模型？走 --shell 或 --call 直接敲工具，一样能取证。"
            )
        return LlmClient(
            base_url=os.environ.get("MYOS_LLM_BASE_URL") or c.get("base_url", ""),
            api_key=api_key,
            model=os.environ.get("MYOS_LLM_MODEL") or c.get("model", ""),
            temperature=float(c.get("temperature", 0.2)),
            timeout=int(c.get("timeout", 180)),
        )

    def _learn_on(self) -> bool:
        """经验沉淀总开关（config.json → agent.learn，默认 true）。

        关掉等于彻底静音：不记录、不召回、不往提示词里塞教训。
        """
        return bool((self.cfg.get("agent") or {}).get("learn", True))

    def _learn_round(self, prompt: str, answer: str, stats: dict):
        """把这一轮的经验落盘（失败教训 + 成功路径）。

        设计上只做「记忆」，不做「改规则」：写的是 `prompts/lessons.md`
        （人可以直接删某一行让它忘掉）和 `logs/memory/qa.jsonl`，
        **绝不覆盖 system.md，也不碰任何闸门判定**——那些是安全边界，
        被"学习"改掉就是灾难。

        整段包在 try 里：学习是锦上添花，不能因为它把一轮结果弄丢。
        """
        if not self._learn_on():
            return
        line = ""
        try:
            line = learn.record_round(BASE_DIR, prompt, answer, stats)
            if line and self.verbose:
                print(f"\n[学到] {line}")
        except Exception as e:
            if self.verbose:
                print(f"\n[教训库写入失败] {type(e).__name__}: {e}")

        # 成功路径：这一轮真的用工具查到了东西、而且没认输，才值得留
        try:
            if (not line and int(stats.get("ok_calls") or 0) > 0
                    and (answer or "").strip()
                    and not GIVEUP_RE.search(answer)):
                learn.remember_success(BASE_DIR, prompt, answer, stats.get("tools"))
        except Exception:
            pass

    def _system_prompt(self) -> str:
        p = BASE_DIR / "prompts" / "system.md"
        if p.exists():
            base = p.read_text(encoding="utf-8")
        else:
            base = "你是电子数据取证分析师，通过 MCP 工具操作本机取证软件。"

        # 身份类提问只答「我是电子数据取证分析师」，不报模型名/版本/接口
        # （用户 2026-11-02 明确要求：只回答身份，不要回答模型）。
        # 所以这里刻意**不**把 llm 配置注入提示词——给了它就会忍不住往外说。

        extra = []
        agents_cfg = self.cfg.get("agent") or {}
        ctx = agents_cfg.get("context")
        if ctx:
            extra.append("\n## 本次会话的补充上下文\n" + str(ctx).strip())

        # ---- 服务器清单 ----
        # 只把「现在能用」的服务器列进主清单。以前把 disabled 的那台也写成
        # 「⏸ xxx：0 个工具（已禁用）」，模型会把这半行和别的服务器混起来，
        # 明明手上有 49 个工具，却言之凿凿地说「服务器被禁用，可用工具数 0」。
        st_rows = self.pool.status()
        live = [s for s in st_rows if s["state"] != "disabled"]
        off = [s for s in st_rows if s["state"] == "disabled"]
        lines = ["\n## 已连接的 MCP 服务器"]
        if live:
            for s in live:
                mark = {"ready": "✅", "error": "❌", "connecting": "…",
                        "idle": "·"}.get(s["state"], "?")
                lines.append(f"- {mark} **{s['name']}**（{s['label']}）：{s['toolCount']} 个工具"
                             + (f" —— {s['error']}" if s["error"] else ""))
        else:
            lines.append("- （一台都没连上）")
        if off:
            lines.append(f"\n未启用的服务器（与本轮无关，不要拿来当理由）："
                         f"{'、'.join(s['name'] for s in off)}")
        lines.append(f"\n工具总数 {len(self.tools)}。调用时用清洗后的名字（点号换成下划线）。")
        if self.tools:
            # 硬锚点：模型最容易犯的错不是调错工具，而是先断言「我没有工具」。
            # 把可用数量写成事实陈述，并规定「不可用」只能引用工具返回值。
            lines.append(
                f"**现在你手上就有 {len(self.tools)} 个工具，随时可调。**"
                f" 说「工具不可用 / 服务器被禁用 / 没有权限」这类话之前，"
                f"必须有 `sys_server_status` 或工具报错原文作依据；"
                f"**没查过就说没有工具，属于编造事实，禁止。**")
        lines.append("某个服务器的工具报「不可用」时，先调 `sys_server_status` 看状态，"
                     "再用 `sys_server_reconnect` 重连，不要盲目重试同一调用。"
                     "显示「已禁用 / 未启用」时**直接就调 `sys_server_reconnect`**——"
                     "它会自己把开关打开并写回配置；不要因此让用户去改 config.json 或重启客户端。")
        lines.append(
            "内置工具（不依赖任何服务器，永远可用）：`sys_server_status`、`sys_server_reconnect`、"
            "`sys_skill_list`、`sys_skill_read`、`sys_skill_exec`、"
            "`sys_file_scan`（本地文件流式扫描：不限大小、不整读内存、UTF-8 与 UTF-16LE 双编码，"
            "返回命中次数 + 字节偏移 + 上下文原文。`ges grep` 撞上 `file_too_large` 时，"
            "用 `ges copy` 把文件复制到本地再交给它）。")
        extra.append("\n".join(lines))

        # ---- 技能清单（渐进披露：只给名字和一句话） ----
        catalog = self.skills.prompt_catalog(max_desc=140)
        if catalog:
            extra.append(
                f"\n## 可用技能（skills）\n"
                f"下面是全部 {len(self.skills.skills)} 个技能的索引——**已经在这里了，"
                f"不要为了「看看有哪些技能」再去调工具**。技能是厂商级操作手册，比单个工具高一层。\n"
                "怎么用：\n"
                "1. **一次只读一个**。从索引里挑最匹配的那一个 `sys_skill_read`，读完照它做；"
                "明显不对路才换第二个。**严禁「挨个读一遍再决定」**——每读一个就烧掉一整轮额度。\n"
                "2. 索引里看不准，用 `sys_skill_list` 传 **keyword** 检索（一次回 3 个候选），"
                "不要反复换词试探。\n"
                "3. 技能里提到的 CLI（ges / thunder-app-cli / bootmagix-cli）用 `sys_skill_exec` 执行，"
                "一次一条命令，禁止管道与 &&。**执行前先看 `sys_skill_read` 回给你的"
                "「本机可执行性」**：写着缺失/不可用，就说明这套 CLI 没装，别浪费轮次去试。\n"
                "4. 不要凭记忆猜技能的命令格式，一切以技能正文为准。\n"
                "5. `sys_skill_exec` 返回以「【」开头的任何内容都代表这次没跑成"
                "（找不到命令 / 已拒绝 / 命令失败）。**同一命令不要再敲第二遍**——"
                "改参数、换技能，或者干脆放弃 CLI，改用 MCP 工具 / 直接基于已有信息下结论。\n"
                "6. 任何工具「同样的参数」调过一次，就不要再来一次；结果不会变。\n\n"
                + catalog
            )
        # 教训库：本项目实测攒下来的「这条路走过不通」，只作提示不当证据。
        # 读不到/没有记录就返回空串，不占 token。
        try:
            if self._learn_on():
                learned = learn.lessons_text(BASE_DIR)
                if learned:
                    extra.append(learned)
        except Exception:
            pass

        return base + "\n".join(extra)

    def close(self):
        self.pool.close()


# ==========================================================================
# CLI
# ==========================================================================
def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"缺少配置文件：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_parser():
    p = argparse.ArgumentParser(
        prog="agent.py",
        description="电子数据取证智能体（MYOS / 超级取证大师）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python agent.py --check                         逐台体检 MCP 服务器
  python agent.py --tools                         列出所有服务器的工具
  python agent.py --skills                        列出本机可用技能（按厂商分组）
  python agent.py --call myos-forensics.query_cases --args "{}"
  python agent.py --skill-run "ges-case-management: ges status"
  python agent.py --shell                         不开模型，直接敲工具（REPL）
  python agent.py -p "看看有哪些案件"              一次性问答
  python agent.py                                  交互对话
""",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    p.add_argument("--check", action="store_true", help="逐台做 MCP 连通性检查")
    p.add_argument("--servers", action="store_true", help="只看 MCP 服务器状态")
    p.add_argument("--tools", action="store_true", help="列出工具（标出所属服务器）")
    p.add_argument("--skills", action="store_true", help="列出本机技能")
    p.add_argument("--skill-run", metavar="'SKILL: CMD'",
                   help='在技能目录执行一条命令，如 "ges-case-management: ges status"')
    p.add_argument("--skill-timeout", type=int, default=300, help="--skill-run 的超时秒数")
    p.add_argument("--json", action="store_true", help="与 --tools / --skills 合用，输出原始 JSON")
    p.add_argument("--call", metavar="TOOL", help="直接调用某个工具")
    p.add_argument("--args", default="{}", help="配合 --call 的工具参数（JSON）")
    p.add_argument("--shell", action="store_true", help="交互式工具命令行（不用模型）")
    p.add_argument("-p", "--prompt", help="一次性提问（需要模型）")
    p.add_argument("--yes", action="store_true", help="跳过破坏性操作的确认（谨慎）")
    p.add_argument("--max-turns", type=int, default=None,
                   help="最多推理轮数（默认取 config.json → agent.max_turns，缺省 40）")
    p.add_argument("--tool-budget", type=int, default=None,
                   help="工具调用总次数上限（默认取 config.json → agent.tool_budget，缺省 120）")
    p.add_argument("--renew-budget", type=int, default=None,
                   help="额度用完后允许自动续期的批数（默认取 config.json → agent.renew_budget）")
    p.add_argument("--save", action="store_true", help="把最终答复存到 reports/")
    p.add_argument("--no-log", action="store_true", help="不写会话日志")
    p.add_argument("--quiet", action="store_true", help="少打印中间过程")
    return p


def resolve_budget(cfg: dict, args) -> tuple[int, int, int]:
    """命令行没显式给，就吃 config.json 里 agent.* 的配置。

    以前 --max-turns 写死 default=20，config 里的 40 被无声忽略，
    交互模式下永远是「20 轮」——用户看到的「只能 20 轮」就是这么来的。
    """
    ag = cfg.get("agent") or {}

    def pick(cli_val, key: str, dflt: int, lo: int, hi: int) -> int:
        v = cli_val
        if v is None:
            try:
                v = int(ag.get(key))
            except (TypeError, ValueError):
                v = dflt
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = dflt
        return max(lo, min(hi, v))

    return (pick(getattr(args, "max_turns", None), "max_turns", 40, 1, 200),
            pick(getattr(args, "tool_budget", None), "tool_budget", 120, 1, 1000),
            pick(getattr(args, "renew_budget", None), "renew_budget", 3, 0, 10))


def print_health(res: dict):
    """打印多服务器体检结果；有 structuredContent 的再做逐项展开。"""
    rows = res.get("servers") or []
    print(res.get("text", ""))
    for r in rows:
        if not r.get("detail"):
            continue
        print(f"\n--- {r['name']} / {r.get('probe')} ---")
        labels = [
            ("ready", "整体就绪"),
            ("forensicDesktopRunning", "取证主程序"),
            ("dbusDaemonRunning", "D-Bus 守护"),
            ("dbusInterfaceInitialized", "D-Bus 接口"),
            ("sessionBusConnected", "会话总线"),
            ("casemanagerRegistered", "CaseManager"),
            ("taskmanagerRegistered", "TaskManager"),
            ("phoneforensicsRegistered", "PhoneForensics"),
            ("probeCallOk", "探针调用"),
        ]
        for key, label in labels:
            if key in r["detail"]:
                mark = "OK  " if r["detail"][key] else "FAIL"
                print(f"  {mark}  {label} ({key})")


def main():
    args = build_parser().parse_args()
    cfg = load_config(Path(args.config))
    if args.yes:
        cfg.setdefault("agent", {})["auto_yes"] = True

    session = Session(enabled=not args.no_log)
    agent = None
    exit_code = 0
    try:
        agent = ForensicsAgent(
            cfg, session,
            auto_yes=bool(args.yes or (cfg.get("agent") or {}).get("auto_yes")),
            allow_destructive=True,
            verbose=not args.quiet,
        )
        ready = agent.pool.ready()
        print(f"[MCP] {len(ready)}/{len(agent.pool.handles)} 台服务器就绪"
              f"（{agent.pool.connect_ms} ms），工具合计 {len(agent.tools)} 个")
        for s in agent.pool.status():
            mark = {"ready": "OK  ", "error": "FAIL", "disabled": "OFF ",
                    "connecting": "…   "}.get(s["state"], "?   ")
            line = f"  {mark} {s['name']:<18} {s['toolCount']:>3} 个工具"
            if s["server"]:
                line += f"   {s['server']} v{s['version']}"
            if s["error"]:
                line += f"   ⚠ {s['error'][:80]}"
            print(line)
        print(f"[技能] {len(agent.skills.skills)} 个"
              f"（带 CLI {sum(1 for s in agent.skills.skills.values() if s.executables)} 个）："
              + "、".join(f"{g['title']}{g['count']}" for g in agent.skills.groups()))

        if args.servers:
            if args.json:
                print(json.dumps(agent.pool.status(), ensure_ascii=False, indent=2))
            else:
                for s in agent.pool.status():
                    line = (f"[{s['state']:<9}] {s['name']:<18} {s['toolCount']:>3} 工具"
                            f"  {s['label']}")
                    if s["server"]:
                        line += f"  ({s['server']} v{s['version']})"
                    print(line)
                    if s["error"]:
                        print(f"            ⚠ {s['error']}")
            return

        if args.skills:
            reg = agent.skills
            if args.json:
                print(json.dumps(reg.groups(), ensure_ascii=False, indent=2))
            else:
                for g in reg.groups():
                    print(f"\n### {g['title']}" + (f" — {g['subtitle']}" if g["subtitle"] else ""))
                    for s in g["skills"]:
                        cli = "  [CLI] " + ", ".join(s["executables"]) if s["executables"] else ""
                        print(f"  · {s['name']}{cli}")
                        print(f"      {s['brief'][:150]}")
            return

        if args.skill_run:
            if ":" not in args.skill_run:
                raise SystemExit('--skill-run 格式："技能名: 命令"，例如 --skill-run "ges-case-management: ges status"')
            sk_name, cmd = args.skill_run.split(":", 1)
            rc, out = agent.skills.exec(sk_name.strip(), cmd.strip(),
                                        timeout=int(args.skill_timeout))
            print(f"[exit={rc}]\n{out}")
            exit_code = 0 if rc == 0 else 1
            return

        if args.tools:
            if args.json:
                print(json.dumps(agent.tools, ensure_ascii=False, indent=2))
            else:
                for i, t in enumerate(agent.tools, 1):
                    req = (t.get("inputSchema") or {}).get("required") or []
                    desc = (t.get("description") or "").replace("\n", " ")
                    print(f"{i:2}. [{t['_server']}] {t['name']}")
                    print(f"    必填：{', '.join(req) or '—'}    {desc[:110]}")
            return

        if args.check:
            print()
            res = agent.health_check()
            print_health(res)
            exit_code = 0 if res.get("ok") else 2
            return

        if args.call:
            try:
                payload = json.loads(args.args)
            except json.JSONDecodeError as e:
                raise SystemExit(f"--args 不是合法 JSON：{e}")
            out = agent.call(args.call, payload, quiet=False)
            print("\n" + out)
            return

        if args.shell:
            print("\n工具命令行。输入 `工具名 {JSON参数}`，例如：")
            print("  myos-forensics.query_cases {}")
            print("  myos-forensics.query_materials {\"caseId\":\"xxx\"}")
            print("输入 :help 看工具名，:q 退出。\n")
            while True:
                try:
                    line = input("tool> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                if line in (":q", ":quit", ":exit"):
                    break
                if line == ":help":
                    for t in agent.tools:
                        print("  " + t["name"])
                    continue
                m = re.match(r"^(\S+)\s*(.*)$", line)
                name, rest = m.group(1), (m.group(2) or "{}").strip()
                try:
                    payload = json.loads(rest or "{}")
                except json.JSONDecodeError as e:
                    print(f"  参数不是合法 JSON：{e}")
                    continue
                out = agent.call(name, payload)
                print("\n" + out + "\n")
            return

        if args.prompt:
            mt, tb, rb = resolve_budget(cfg, args)
            answer, _ = agent.run(args.prompt, max_turns=mt, tool_budget=tb, renew_budget=rb)
            print("\n" + "=" * 60 + "\n" + answer)
            if args.save:
                p = session.save_report(answer)
                print(f"\n[报告已保存] {p}")
            return

        # 交互对话（保留上下文）
        print("\n取证智能体已就绪。直接说需求；:q 退出  :health 体检  :tools 看工具  :reset 清空上下文")
        history = None
        while True:
            try:
                line = input("\n取证> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in (":q", ":quit", ":exit"):
                break
            if line == ":health":
                print_health(agent.health_check())
                continue
            if line == ":tools":
                for t in agent.tools:
                    print("  " + t["name"])
                continue
            if line == ":reset":
                history = None
                print("  上下文已清空。")
                continue
            if history is None:
                history = agent.new_history()
            mt, tb, rb = resolve_budget(cfg, args)
            answer, history = agent.run(line, max_turns=mt, tool_budget=tb,
                                        renew_budget=rb, history=history)
            print("\n" + answer)
            if args.save:
                p = session.save_report(answer)
                print(f"\n[报告已保存] {p}")
    except McpError as e:
        print(f"\n[MCP 错误] {e}", file=sys.stderr)
        print("  提示：先启动「超级取证大师」(ForensicDesktop.exe)，"
              "再重连；或跑 python agent.py --check 定位。", file=sys.stderr)
        exit_code = 1
    except LlmError as e:
        print(f"\n[模型错误] {e}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        if agent:
            agent.close()
        session.close()
        if session.path and session.path.exists():
            print(f"\n[会话日志] {session.path}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
