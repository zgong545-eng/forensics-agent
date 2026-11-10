# -*- coding: utf-8 -*-
"""
技能（Skills）系统 —— 发现 / 解析 / 加载 / 执行。

设计要点
--------
1. **渐进披露**：system prompt 里只放「名称 + 一句话描述」的清单，
   模型判断需要某技能时再调 `skill_read` 拉全文，避免一次性撑爆上下文。
2. **零依赖**：手写 YAML frontmatter 解析器（只取顶层 key），不引 PyYAML。
3. **可执行**：技能目录里若带 CLI（ges.exe / thunder-app-cli.exe / bootmagix-cli.exe…），
   模型可通过 `skill_exec` 在技能目录内执行，命令首词走白名单校验。
4. **厂商归类**：读 frontmatter 的 author / tags 自动分到 弘连 / 美亚柏科 / 其它，
   将来往 roots 里丢新厂商的技能包，无需改代码。

对外接口
--------
    reg = SkillRegistry(roots=[...], allow_commands=[...])
    reg.scan()
    reg.list()                    -> 供 UI 用
    reg.prompt_catalog()          -> 供 system prompt 用
    reg.read(name)                -> 全文 + 附件清单
    reg.exec(name, command, ...)  -> (rc, output)
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 厂商归类
# --------------------------------------------------------------------------
VENDORS = {
    "honglian": ("弘连", "火眼 GoldenEyes / 雷电 AppAnalyzer / 火眼仿真 BootMagix"),
    "meiya": ("美亚柏科", "DC 取证 / 手机取证大师"),
    "pico": ("美亚柏科", "DC 取证 / 手机取证大师"),
    "sangfor": ("深信服", ""),
    "qianxin": ("奇安信", ""),
    "generic": ("通用取证", "方法论 / 第三方工具链"),
}

# author / tags / 描述里出现这些词就判厂商
_VENDOR_HINTS = [
    (("honglian", "弘连", "火眼", "goldeneyes", "雷电", "bootmagix",
      "thunder-app", "appanalyzer"), "honglian"),
    (("meiya", "美亚", "pico", "取证大师dc"), "meiya"),
    (("sangfor", "深信服"), "sangfor"),
    (("qianxin", "奇安信"), "qianxin"),
]

# 非取证类的技能按主题归到「安全研究」
_SECURITY_HINTS = (
    "ctf", "pwn", "crypto", "stego", "reverse", "exploit", "steganography",
    "mt19937", "go-binary", "unicorn", "heap",
)

# 判定「这条命令可能在改数据」的动词，命中就走人工确认
WRITE_VERBS = (
    "import", "add", "create", "open", "delete", "del", "remove", "rm",
    "close", "export", "decrypt", "apply", "install", "upgrade", "start",
    "stop", "reboot", "reset", "write", "set", "submit", "run", "kill",
    "mount", "unmount", "hash", "rename", "move", "copy", "cp", "mv",
)


# 通用解释器 / Shell：sys_skill_exec 一律不执行。
# 模型想「现写个小脚本处理数据」时最爱用它们，而这类请求 100% 被拒，
# 拒完它还会换引号再试——三次就把整轮对话拖到「连续报错」早停。
_INTERPRETERS = {
    "python", "python3", "python.exe", "py", "py.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "cmd", "cmd.exe", "bash", "bash.exe", "sh", "zsh",
    "node", "node.exe", "perl", "php", "ruby", "wscript", "cscript",
}


# 技能名前缀也是强信号（很多技能包不带 author，但命名规范稳定）
_NAME_PREFIX = [
    (("ges-", "ges_", "honglian-", "thunder-", "bootmagix-"), "honglian"),
    (("meiya-", "dc-", "pico-"), "meiya"),
]


def guess_vendor(author: str, tags: list[str], description: str,
                 body_hint: str = "", name: str = "") -> str:
    """分层判厂商：author > 技能名前缀 > tags > description > 正文。

    分层是为了避免「正文里顺带提到 ges/火眼」就被误判成弘连厂商技能。
    """
    def hit(hay: str):
        hay = (hay or "").lower()
        for words, key in _VENDOR_HINTS:
            if any(w in hay for w in words):
                return key
        return None

    if author:
        k = hit(author)
        if k:
            return k
    low = (name or "").lower()
    for prefixes, key in _NAME_PREFIX:
        if low.startswith(prefixes):
            return key
    for t in (tags or []):
        k = hit(str(t))
        if k:
            return k
    k = hit(description)
    if k:
        return k
    # 正文兜底：至少命中两次，且两次命中是不同词
    body_hint = (body_hint or "").lower()
    for words, key in _VENDOR_HINTS:
        found = {w for w in words if w in body_hint}
        if len(found) >= 2:
            return key
    hay = " ".join([author or "", " ".join(tags or []), description or ""]).lower()
    if any(w in hay for w in _SECURITY_HINTS):
        return "security"
    return "generic"


VENDORS["security"] = ("安全研究", "CTF / 逆向 / 密码学")


def _vendor_title(vendor: str) -> str:
    """分组标题。已知厂商查表，未知的直接用目录名（专题技能包就是这么归的）。"""
    if vendor in VENDORS:
        return VENDORS[vendor][0]
    return vendor or VENDORS["generic"][0]



# --------------------------------------------------------------------------
# frontmatter 解析（只取顶层 key，够用且不引依赖）
# --------------------------------------------------------------------------
_SCALAR_KEYS = re.compile(r"(?m)^([A-Za-z_][\w-]*):")


def _unquote(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        inner = val[1:-1]
        if val[0] == "'":
            inner = inner.replace("''", "'")
        # 折叠换行：YAML 流式标量里换行等价空格
        inner = re.sub(r"\s*\n\s*", " ", inner)
        return inner.strip()
    # 未加引号的多行值：同样折叠
    return re.sub(r"\s*\n\s*", " ", val).strip()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (元数据字典, 正文)。没有 frontmatter 时返回 ({}, 原文)。"""
    if not text.lstrip().startswith("---"):
        return {}, text
    stripped = text.lstrip()
    end = stripped.find("\n---", 3)
    if end < 0:
        return {}, text
    fm = stripped[3:end]
    body = stripped[end + 4:].lstrip("\n")

    keys = [(m.start(), m.group(1)) for m in _SCALAR_KEYS.finditer(fm)]
    data: dict = {}
    for i, (pos, key) in enumerate(keys):
        nxt = keys[i + 1][0] if i + 1 < len(keys) else len(fm)
        seg = fm[pos:nxt]
        raw = seg.split(":", 1)[1] if ":" in seg else ""
        if raw.strip().startswith("[") and "]" in raw:
            inner = raw.strip()[1: raw.strip().index("]")]
            data[key] = [_unquote(x) for x in inner.split(",") if x.strip()]
        else:
            data[key] = _unquote(raw)
    return data, body


def _extract_bins(fm_text: str) -> list[str]:
    m = re.search(r"bins:\s*\[([^\]]*)\]", fm_text)
    if not m:
        m = re.search(r"bins:\s*\n((?:\s*-\s*\S+\n?)+)", fm_text)
        if m:
            return [x.strip().lstrip("-").strip() for x in m.group(1).splitlines() if x.strip()]
        return []
    return [_unquote(x) for x in m.group(1).split(",") if x.strip()]


# 这批专题技能包的 frontmatter 是模板灌出来的，description 基本都写成
# 「任何司法与取证工作都应该被触发」——25 个技能描述一模一样，模型没法据此挑，
# 只能挨个 sys_skill_read 去试，一轮就烧掉一次额度。命中这类套话就回退到正文概述。
_BOILERPLATE = (
    "任何司法与取证工作都应该被触发",
    "任何涉及文件操作的任务都应该被触发",
    "任何涉及AI",
    "任何司法、取证",
    "都应该被触发",
)
_SUMMARY_HEADS = ("技能概述", "概述", "简介", "技能简介", "适用场景", "核心用途")


def _body_summary(body: str, limit: int = 130) -> str:
    """从正文里抽第一段实质说明，优先「技能概述」小节。"""
    lines = body.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        h = ln.strip().lstrip("#").strip()
        if any(h.startswith(k) for k in _SUMMARY_HEADS):
            start = i + 1
            break

    buf: list[str] = []
    for ln in lines[start:]:
        s = ln.strip()
        if not s or s in ("---", "***", "___", "```"):
            if buf:
                break
            continue
        if s.startswith("#") or s.startswith("|") or s.startswith("###"):
            if buf:
                break
            continue
        if s.startswith("- ") or s.startswith("* "):
            if buf:
                break
            continue
        buf.append(s)
        if sum(len(x) for x in buf) >= limit:
            break
    return re.sub(r"\s+", " ", " ".join(buf)).strip()[:limit]


def _clean_desc(fm_desc: str, body: str, fallback: str) -> str:
    """description 是套话就用正文概述，正文也没有才退回原值。"""
    raw = (fm_desc or "").strip()
    cleaned = raw
    for bad in _BOILERPLATE:
        cleaned = cleaned.replace(bad, "")
    cleaned = re.sub(r"^[、，,;；/\\\s]+|[、，,;；/\\\s]+$", "", cleaned)
    usable = (len(cleaned) >= 12
              and "都应该被触发" not in cleaned
              and not cleaned.startswith("任何"))
    if usable:
        return cleaned
    return _body_summary(body) or raw or fallback


# --------------------------------------------------------------------------
# 技能对象
# --------------------------------------------------------------------------
@dataclass
class Skill:
    name: str
    path: Path                 # SKILL.md 路径
    description: str = ""
    author: str = ""
    vendor: str = "generic"
    tags: list = field(default_factory=list)
    bins: list = field(default_factory=list)
    allowed_tools: str = ""
    body: str = ""
    executables: list = field(default_factory=list)
    extras: list = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def title(self) -> str:
        return _vendor_title(self.vendor)

    def brief(self, limit: int = 160) -> str:
        d = re.sub(r"\s+", " ", self.description or "").strip()
        return d if len(d) <= limit else d[: limit - 1] + "…"

    def to_dict(self, with_body: bool = False) -> dict:
        d = {
            "name": self.name,
            "title": self.title,
            "vendor": self.vendor,
            "author": self.author,
            "description": self.description,
            "brief": self.brief(400),
            "tags": self.tags,
            "bins": self.bins,
            "executables": self.executables,
            "extras": self.extras,
            "hasCli": bool(self.executables),
            "dir": str(self.dir),
            "reliable": not self.description.lower().startswith("请填写"),
        }
        if with_body:
            d["body"] = self.body
            d["path"] = str(self.path)
        return d


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
def _split_command(cmd: str) -> list[str]:
    """把命令行拆成 argv。不用 shell —— Windows 的 cmd 会把 `bin/ges.exe`
    的 `/` 当参数分隔符，直接报「'bin' 不是内部或外部命令」。"""
    try:
        toks = shlex.split(cmd, posix=False)
    except ValueError:
        toks = cmd.split()
    out = []
    for t in toks:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        out.append(t)
    return out


def _kill_tree(proc: subprocess.Popen) -> None:
    """连锅端：杀进程树，不是只杀第一层。

    厂商 CLI（ges / thunder-app-cli）本身是个启动器，真正干活的是它拉起来的
    子进程，而子进程继承了 stdout 管道。只 kill 父进程的话管道不会关，
    `communicate()` 会一直等到孙进程自己退出——「点了中断还得干等十几秒」
    就是这么来的。Windows 用 taskkill /T，POSIX 用进程组。
    """
    pid = proc.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _decode(raw: bytes) -> str:
    """Windows 控制台工具多半输出 GBK，先试 UTF-8 再退 GB18030。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class SkillRegistry:
    def __init__(self, roots=None, allow_commands=None, max_body_chars: int = 24000):
        self.roots = [Path(r) for r in (roots or [])]
        self.allow_commands = list(allow_commands or [])
        self.max_body_chars = max_body_chars
        self.skills: dict[str, Skill] = {}
        self.errors: list[dict] = []

    # ---------------- 发现 ----------------
    def _candidates(self, root: Path) -> list[tuple[Path, str]]:
        """收集 SKILL.md，返回 [(路径, 分组名)]。

        支持三种布局：
            root/SKILL.md                → 分组 ""
            root/技能/SKILL.md            → 分组 ""        （厂商技能包，靠 frontmatter 归类）
            root/分组/技能/SKILL.md        → 分组 "分组名"   （专题技能包，靠目录名归类）
        已经识别为技能目录的，不再往里钻，免得把技能内部的附件当技能。
        """
        out: list[tuple[Path, str]] = []
        if not root.is_dir():
            return out
        if (root / "SKILL.md").is_file():
            return [(root / "SKILL.md", "")]
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if (child / "SKILL.md").is_file():
                out.append((child / "SKILL.md", ""))
                continue
            for sub in sorted(child.iterdir()):
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    out.append((sub / "SKILL.md", child.name))
        return out

    def scan(self) -> "SkillRegistry":
        self.skills.clear()
        self.errors.clear()
        for root in self.roots:
            if not root.exists():
                self.errors.append({"root": str(root), "error": "目录不存在"})
                continue
            for md, group in self._candidates(root):
                try:
                    sk = self._load(md, group=group)
                except Exception as e:
                    self.errors.append({"path": str(md), "error": f"{type(e).__name__}: {e}"})
                    continue
                # 同名以先扫到的为准（roots 顺序即优先级）
                self.skills.setdefault(sk.name, sk)
        return self

    def _load(self, md: Path, group: str = "") -> Skill:
        text = md.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        name = str(meta.get("name") or md.parent.name).strip().strip('"').strip("'")
        desc = str(meta.get("description") or "")
        author = str(meta.get("author") or "").strip().strip('"').strip("'")
        if not author:
            # author 常写在 metadata: 缩进块里，顶层解析抓不到，兜底再搜一次
            m = re.search(r"(?m)^\s+author:\s*(\S+)", text[:3000])
            if m:
                author = _unquote(m.group(1))
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        bins = _extract_bins(text[:2000])

        # 目录里的可执行文件 + 附带资源（只认明确的可执行后缀，避免把 LICENSE 当 CLI）
        exes, extras = [], []
        for f in sorted(md.parent.rglob("*")):
            if not f.is_file() or f == md:
                continue
            rel = f.relative_to(md.parent).as_posix()
            if f.suffix.lower() in (".exe", ".cmd", ".bat", ".ps1"):
                exes.append(rel)
            elif f.suffix.lower() in (".md", ".json", ".yaml", ".yml", ".py"):
                extras.append(rel)

        body = body[: self.max_body_chars] if len(body) > self.max_body_chars else body
        # frontmatter 只写套话时，从正文概述里取一句真的描述
        desc = _clean_desc(desc, body, md.parent.name)

        vendor = guess_vendor(author, tags, desc, body[:3000], name)
        # 专题技能包（root/分组/技能/）里的成员大多不带 author，
        # 判成「通用取证」就散了，统一按分组目录名归堆
        if group and vendor == "generic":
            vendor = group

        return Skill(
            name=name, path=md, description=desc, author=author,
            vendor=vendor, tags=tags, bins=bins,
            allowed_tools=str(meta.get("allowed-tools") or ""),
            body=body, executables=exes, extras=extras[:40],
        )

    # ---------------- 查询 ----------------
    def get(self, name: str) -> Skill | None:
        if name in self.skills:
            return self.skills[name]
        low = (name or "").strip().lower()
        for k, v in self.skills.items():
            if k.lower() == low:
                return v
        # 允许前缀模糊（模型常把 ges-case-management 写成 ges_case_management）
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        for k, v in self.skills.items():
            if norm(k) == norm(name):
                return v
        # 模型经常用 **CLI 名** 当技能名（`sys_skill_exec name:"ges"`）。
        # 以前这里返回 None，它收到的是一句「未找到技能 ges」——听起来像环境坏了，
        # 其实只是名字不同（技能叫 ges-case-analysis，CLI 才叫 ges）。
        # 按声明的 bins / 可执行文件名反查，一次就能对上。
        for k, v in self.skills.items():
            bins = {Path(b).name.lower() for b in (v.bins or [])}
            exes = {Path(e).name.lower() for e in (v.executables or [])}
            if low in bins or low in exes:
                return v
        return None

    def list(self, vendor: str | None = None) -> list[dict]:
        out = []
        for sk in sorted(self.skills.values(), key=lambda s: (s.vendor, s.name)):
            if vendor and sk.vendor != vendor:
                continue
            out.append(sk.to_dict())
        return out

    # ---------------- 检索 ----------------
    def search(self, query: str, limit: int = 3) -> list[dict]:
        """按关键词给技能打分，返回最匹配的几个（带 score）。

        存在的理由：全部技能索引本来就已经注入系统提示了，模型不需要挨个
        `sys_skill_read` 去「试哪个对路」——那样每读一个就烧掉一整轮额度。
        一次检索拿到前几名，只读最匹配的那一个就够了。
        """
        q = (query or "").strip().lower()
        if not q:
            return []

        terms: set[str] = set()
        for w in re.split(r"[\s,，、;；/|·]+", q):
            if w:
                terms.add(w)
        # 中文没空格，补 2-gram，免得整句匹配不上任何描述
        for seg in re.findall(r"[\u4e00-\u9fa5]+", q):
            if len(seg) <= 4:
                terms.add(seg)
            else:
                terms.update(seg[i:i + 2] for i in range(len(seg) - 1))
                terms.add(seg)

        rows = []
        for sk in self.skills.values():
            name_l = sk.name.lower()
            desc_l = (sk.description or "").lower()
            tags_l = " ".join(str(t) for t in sk.tags).lower()
            body_l = (sk.body or "").lower()[:4000]
            score = 0
            for t in terms:
                if t in name_l:
                    score += 10
                if t in tags_l:
                    score += 6
                if t in desc_l:
                    score += 3
                if t in body_l:
                    score += 1
            if score:
                rows.append((score, sk))

        rows.sort(key=lambda x: (-x[0], x[1].name))
        out = []
        for score, sk in rows[:max(1, limit)]:
            d = sk.to_dict()
            d["score"] = score
            out.append(d)
        return out

    def groups(self) -> list[dict]:
        """按厂商 / 专题分组，供 UI 渲染。

        顺序：厂商组（弘连、美亚）→ 专题组（按目录名）→ 通用 / 安全兜底。
        专题排中间是因为它们往往是成套导入的，数量大，得让人一眼看到。
        """
        buckets: dict[str, list] = {}
        for sk in sorted(self.skills.values(), key=lambda s: s.name):
            buckets.setdefault(sk.vendor, []).append(sk.to_dict())
        head = ["honglian", "meiya"]
        tail = ["generic", "security"]
        order = [k for k in head if k in buckets]
        order += sorted(k for k in buckets if k not in head and k not in tail)
        order += [k for k in tail if k in buckets]
        rows = []
        for key in order:
            title = _vendor_title(key)
            sub = VENDORS.get(key, ("", ""))[1] if key in VENDORS else "专题技能包"
            rows.append({"vendor": key, "title": title, "subtitle": sub,
                         "skills": buckets[key], "count": len(buckets[key])})
        return rows

    # ---------------- 给 system prompt 的清单 ----------------
    def prompt_catalog(self, max_desc: int = 150) -> str:
        if not self.skills:
            return ""
        lines = []
        for g in self.groups():
            lines.append(f"### {g['title']}（{g['count']} 个）")
            for s in g["skills"]:
                cli = ""
                if s["executables"]:
                    cli = "  [CLI: " + ", ".join(s["executables"][:2]) + "]"
                brief = s["brief"]
                if len(brief) > max_desc:
                    brief = brief[: max_desc - 1] + "…"
                lines.append(f"- **{s['name']}**：{brief}{cli}")
            lines.append("")
        return "\n".join(lines).rstrip()

    # ---------------- 读取 ----------------
    def probe_cli(self, sk: Skill) -> list[dict]:
        """这个技能声明的 CLI，在本机到底能不能用。

        读技能时就把结论写进去，模型才不会读完手册照着敲一堆必然 127 的命令
        （那正是「同一个命令换着花样重试」的来源）。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for e in sk.executables:
            base = Path(e).name
            if base in seen:
                continue
            seen.add(base)
            p = Path(e)
            cand = p if p.is_absolute() else (sk.dir / p)
            out.append({"name": base, "ok": cand.is_file(), "path": str(cand)})
        for b in sk.bins:
            if b in seen:
                continue
            seen.add(b)
            found = (shutil.which(b)
                     or shutil.which(b, path=str(sk.dir) + os.pathsep + str(sk.dir / "bin")))
            out.append({"name": b, "ok": bool(found), "path": found or ""})
        return out

    def read(self, name: str) -> str:
        sk = self.get(name)
        if not sk:
            near = [k for k in self.skills if name and name[:4].lower() in k.lower()]
            hint = ("；相近的有：" + "、".join(near[:5])) if near else ""
            return f"【未找到技能】{name}{hint}。先用 skill_list 查看可用技能。"
        parts = [
            f"# 技能：{sk.name}（{sk.title}）",
            f"目录：{sk.dir}",
        ]
        if sk.executables:
            parts.append("可执行文件：" + "、".join(sk.executables))
        if sk.bins:
            parts.append("依赖命令：" + "、".join(sk.bins))
        if sk.extras:
            parts.append("附带文件：" + "、".join(sk.extras[:12]))

        # ---- 本机可执行性体检 ----
        probe = self.probe_cli(sk)
        if probe:
            ok = [p["name"] for p in probe if p["ok"]]
            bad = [p["name"] for p in probe if not p["ok"]]
            parts.append("本机可执行性：" + ("可用 " + "、".join(ok) if ok else "全部不可用"))
            if bad:
                parts.append(f"  缺失：{'、'.join(bad)}")
            if not ok:
                parts.append("  ⚠️ 这套 CLI 在本机没装/没解析到，**不要调用 sys_skill_exec 跑它**——"
                             "必然「找不到命令」。此技能只当方法论参考，改用 MCP 工具或直接给结论。")
        else:
            parts.append("本机可执行性：此技能没有自带 CLI（纯方法论）。"
                         "不要用 sys_skill_exec 去敲它的示例命令，那是厂商软件里的操作，不是命令行。")
        parts.append("")
        parts.append("--- SKILL.md 全文 ---")
        parts.append(sk.body)
        return "\n".join(parts)

    # ---------------- 执行 ----------------
    def is_write_command(self, command: str) -> bool:
        low = command.lower()
        for v in WRITE_VERBS:
            if re.search(r"(?:^|[\s/\"'-])" + re.escape(v) + r"(?:[\s\"']|$)", low):
                return True
        return False

    def check_command(self, sk: Skill, command: str) -> tuple[bool, str]:
        """命令首词是否被允许。返回 (ok, 说明)。"""
        cmd = (command or "").strip()
        if not cmd:
            return False, "命令为空"
        if len(cmd) > 4000:
            return False, "命令过长"
        # 危险 shell 组合直接拒
        for bad in ("&&", "||", ";", "`", "$(", "\n", ">", "<", "|"):
            if bad in cmd:
                return False, f"命令含不被允许的 shell 控制符「{bad}」，请一次只执行一条命令"
        first = cmd.split()[0].strip('"').strip("'")
        base = Path(first).name
        # 1) 技能目录内的可执行文件
        allowed = {Path(e).name for e in sk.executables}
        if base in allowed:
            return True, ""
        # 2) 白名单命令
        if base in {Path(c).name for c in self.allow_commands}:
            return True, ""
        # 3) PATH 里能解析到的同名命令，且在白名单（防止写 `calc` 之类的绝对路径逃逸）
        if base.lower() in _INTERPRETERS:
            # 这一类是最常见的「撞墙」：模型想用 python/powershell 现写脚本处理数据。
            # 只拒不给方向，它会换个引号再试一遍，三次就把整轮对话拖停。
            return False, (
                f"命令「{base}」是通用解释器/Shell，本工具不执行这类命令。"
                f"sys_skill_exec 只能跑技能目录自带的 CLI（{sorted(allowed) or '无'}）。"
                f"要读镜像或插件库里的数据，请改用 MCP 工具（query_cases / load_case / "
                f"sqlite_query / global_search_content / quick_search）；"
                f"要跑厂商 CLI，就抄技能正文里写好的那条命令。"
            )
        return False, (
            f"命令「{base}」不在白名单。本技能可执行：{sorted(allowed) or '无'}；"
            f"全局白名单：{self.allow_commands}。"
            f"如需扩展，请改 config.json 的 skills.allow_commands。"
        )

    def exec(self, name: str, command: str, timeout: int = 300,
             should_stop=None) -> tuple[int, str]:
        """执行技能命令。

        should_stop 是个无参回调：返回真就杀掉子进程。用于「用户点了中断」——
        厂商 CLI 动辄跑十几秒到几分钟（ges 解析、导出），不能让人干等它跑完。
        """
        sk = self.get(name)
        if not sk:
            return 127, f"【未找到技能】{name}"
        # 先确认这个技能的 CLI 到底在不在本机——不在就直接说死，别让模型反复试
        probe = self.probe_cli(sk)
        if probe and not any(p["ok"] for p in probe):
            return 127, ("【无法执行】技能「" + sk.name + "」依赖的 CLI 在本机都解析不到："
                         + "、".join(p["name"] for p in probe)
                         + "。换参数也没用，sys_skill_exec 这条路已走不通——"
                         "把这条技能当方法论参考，改用 MCP 工具或直接给结论。")
        ok, why = self.check_command(sk, command)
        if not ok:
            return 126, f"【已拒绝】{why}"

        argv = _split_command(command)
        if not argv:
            return 126, "【已拒绝】命令为空"

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # 让技能目录（含 bin/）里的 CLI 能被直接按名字调用
        env["PATH"] = str(sk.dir) + os.pathsep + str(sk.dir / "bin") + os.pathsep + env.get("PATH", "")

        first = argv[0]
        exe = None
        cand = Path(first)
        if cand.is_absolute() and cand.is_file():
            exe = cand
        elif (sk.dir / first).is_file():
            exe = sk.dir / first
        else:
            found = shutil.which(first, path=env["PATH"])
            if found:
                exe = Path(found)
        if exe is None:
            return 127, (f"【找不到命令】{first}\n"
                         f"已把技能目录与 bin/ 加入 PATH，仍然解析不到。\n"
                         f"该技能可执行文件：{sk.executables or '无'}")

        try:
            popen_kw = {}
            if os.name == "nt":
                # 新进程组：taskkill /T 才能顺藤摸瓜找到子孙；也免得 Ctrl+C 串到主进程
                popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(
                [str(exe)] + argv[1:],
                cwd=str(sk.dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_kw,
            )
        except Exception as e:
            return 1, f"【执行失败】{type(e).__name__}: {e}"

        # 边等边瞄一眼有没有被叫停。0.35s 的粒度既不会把 CPU 吃满，
        # 又能让「点中断 → 真的停下来」在一秒内发生。
        killed = ""
        deadline = time.time() + timeout
        while True:
            try:
                raw_out, raw_err = proc.communicate(timeout=0.35)
                break
            except subprocess.TimeoutExpired:
                if should_stop is not None:
                    try:
                        want = bool(should_stop())
                    except Exception:
                        want = False
                    if want:
                        _kill_tree(proc)
                        raw_out, raw_err = proc.communicate()
                        killed = "user"
                        break
                if time.time() > deadline:
                    _kill_tree(proc)
                    proc.communicate()
                    return 124, f"【超时】命令执行超过 {timeout}s 已终止：{command}"

        if killed:
            # 用【】开头：这是「没拿到结果」，进「连续报错」计数合理
            return 130, (f"【已中断】用户手动停止了这条命令：{command}\n"
                         f"（子进程已杀掉，别用同样的参数重试，先问用户想怎么做）")

        out = _decode(raw_out or b"")
        err = _decode(raw_err or b"")
        text = out.strip()
        if err.strip():
            text += ("\n--- stderr ---\n" + err.strip()) if text else err.strip()
        if not text:
            text = "（无输出）"
        if len(text) > 12000:
            text = text[:12000] + f"\n…（已截断，原长 {len(text)} 字符）"
        head = f"[exit={proc.returncode}] {command}  ({exe})"
        if proc.returncode != 0:
            # 非零退出 = 这次调用没拿到东西。用【】开头，让 agent 的进展检测
            # 能把它算进「连续报错」，而不是傻等到「同参数三次」才停。
            return proc.returncode, f"【命令失败 exit={proc.returncode}】{command}\n{text}"
        return proc.returncode, head + "\n" + text

    # ---------------- 自检 ----------------
    def summary(self) -> dict:
        return {
            "roots": [str(r) for r in self.roots],
            "rootDetail": self.root_detail(),
            "count": len(self.skills),
            "withCli": sum(1 for s in self.skills.values() if s.executables),
            "vendors": {g["title"]: g["count"] for g in self.groups()},
            "errors": self.errors,
        }

    def root_detail(self) -> list[dict]:
        """每个扫描目录的实际路径 / 是否存在 / 各贡献了几个技能。

        用户看「技能到底从哪来的」就靠这个——同名技能按 roots 顺序取第一个，
        所以顺序也有意义，一并标出来。
        """
        out = []
        for i, root in enumerate(self.roots, 1):
            mine = [s for s in self.skills.values() if _under(s.path, root)]
            out.append({
                "order": i,
                "path": str(root),
                "exists": Path(root).is_dir(),
                "count": len(mine),
                "skills": [s.name for s in sorted(mine, key=lambda x: x.name)][:60],
            })
        return out


def _under(p: Path, root: Path) -> bool:
    try:
        Path(p).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


if __name__ == "__main__":
    import json
    import sys

    cfg_path = Path(__file__).with_name("config.json")
    roots, allow = [], []
    if cfg_path.exists():
        c = json.loads(cfg_path.read_text(encoding="utf-8"))
        sc = c.get("skills") or {}
        roots = [os.path.expandvars(os.path.expanduser(r)) for r in (sc.get("roots") or [])]
        allow = sc.get("allow_commands") or []
    if not roots:
        roots = [str(Path.home() / ".workbuddy" / "skills")]

    reg = SkillRegistry(roots, allow).scan()
    if "--json" in sys.argv:
        print(json.dumps(reg.groups(), ensure_ascii=False, indent=2))
    elif "--prompt" in sys.argv:
        print(reg.prompt_catalog())
    else:
        print(json.dumps(reg.summary(), ensure_ascii=False, indent=2))
        print()
        print(reg.prompt_catalog())
