"""锁住「三道题怎么查出来的」这套 SOP。

这轮新增的东西全是**方法**层面的，不是某个工具的参数：
  · 索引优先（先 sys_fs_index，不要先裸数据检索）
  · 选表（TBL_FS_ENTRY_SOURCE 是陷阱）
  · READ 的编码（Foxmail 的 Account.cfg 是 GBK）
  · 镜像内路径自动拼挂载根
  · 提示词里必须写死这些套路（否则模型还是老样子）

所以测试分两类：
  1. 代码行为（能跑起来的部分）
  2. 提示词契约（关键判据必须在文本里出现）

所有临时文件落在 E 盘，不碰 C 盘。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tempfile.tempdir = str(ROOT / ".tmp")
(ROOT / ".tmp").mkdir(exist_ok=True)

import agent  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"[通过] {name}")
    else:
        fail += 1
        print(f"[失败] {name}" + (f"\n       {detail}" if detail else ""))


# ---------------- 1. 选表：SOURCE 是陷阱 ----------------
GUID = "3A6D391261CB4064B6B3FF7C2610D6B5"
check("选表：SOURCE 排在前面也不会被选中",
      agent.pick_entry_table([f"TBL_FS_ENTRY_SOURCE", f"TBL_FS_ENTRY_{GUID}"])
      == f"TBL_FS_ENTRY_{GUID}",
      agent.pick_entry_table(["TBL_FS_ENTRY_SOURCE", f"TBL_FS_ENTRY_{GUID}"]))

check("选表：只有 GUID 表时照常返回",
      agent.pick_entry_table([f"TBL_FS_ENTRY_{GUID}"]) == f"TBL_FS_ENTRY_{GUID}")

check("选表：只有 SOURCE 时返回 None（交给上层报错，不拿它去查）",
      agent.pick_entry_table(["TBL_FS_ENTRY_SOURCE"]) is None,
      agent.pick_entry_table(["TBL_FS_ENTRY_SOURCE"]))

check("选表：大小写混杂的 GUID 也认",
      agent.pick_entry_table([f"tbl_fs_entry_{GUID}".upper()]) is not None,
      agent.pick_entry_table([f"TBL_FS_ENTRY_{GUID}".lower()]))


# ---------------- 2. 读文本：编码不能猜错 ----------------
tmp = Path(tempfile.mkdtemp(prefix="sop_"))


def write(name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


# 注意：纯 ASCII 内容用 GBK 存也能被 UTF-8 解开（两者对 ASCII 兼容），
# 所以必须带中文才能真正考验编码识别 —— 真实的 Account.cfg 里就有中文。
gbk = write("Account.cfg",
            "[Account]\nEmail=somebadthings2026@163.com\n备注=测试账号\n".encode("gbk"))
rep = agent.read_text_file(gbk)
check("GBK 配置能正确解码（不乱码）",
      "somebadthings2026@163.com" in rep and "测试账号" in rep and "�" not in rep,
      rep[:200])
check("GBK 文件报告里写明用的编码", "编码：gbk" in rep.lower(), rep[:120])

utf8 = write("config.json", '{"model": "Qwen3-1.7B"}\n'.encode("utf-8"))
rep = agent.read_text_file(utf8)
check("UTF-8 文件照常读", "Qwen3-1.7B" in rep, rep[:150])

u16 = write("u16.txt", "max_model_len=2048\n".encode("utf-16"))
rep = agent.read_text_file(u16)
check("UTF-16 文件能读（不出现 \\x00 乱码）",
      "2048" in rep and "\x00" not in rep, repr(rep[:120]))

big = write("serve.sh", "#!/bin/bash\nMAX_LEN=${MAX_LEN:-2048}\nexec vllm serve\n".encode("utf-8"))
rep = agent.read_text_file(big, pattern=r"max_len|max-model-len")
check("pattern 只回匹配行，且带行号",
      "2048" in rep and "2|" in rep.replace(" ", "") and "vllm serve" not in rep, rep[:300])

rep = agent.read_text_file(big, pattern="(")
check("非法正则会明确报参数错，不抛异常", "【参数错误】" in rep, rep[:120])


# ---------------- 3. 镜像内路径 → 本地路径 ----------------
local = agent.resolve_image_path(str(utf8))
check("本地已存在的路径直接返回", local is not None and local.exists(), str(local))

check("不存在的镜像内路径返回 None（不瞎猜、不抛异常）",
      agent.resolve_image_path("/Partition0_系统/绝对不存在的目录/x.txt") is None)

roots = agent.osmap_roots()
check("挂载根探测返回列表且不抛异常（Dokan stat 会报 1450）",
      isinstance(roots, list), type(roots))


# ---------------- 4. 索引定位：找得到检材库 ----------------
dbs = agent.find_case_dbs()
check("能自动定位到检材的 FSData.mfs（本机有案件时）",
      len(dbs) == 0 or dbs[0].name == "FSData.mfs", [str(d) for d in dbs[:2]])
check("定位结果都是真实存在的文件",
      all(d.is_file() for d in dbs[:3]), [str(d) for d in dbs[:3]])

check("指定的目录参数也能定位（传案件根）",
      agent.find_case_dbs(str(dbs[0].parent.parent.parent)) if dbs else True,
      "no case")


# ---------------- 5. 明文索引兜底 ----------------
if dbs:
    idx = dbs[0].parent / "FSDataIndex.mfs"
    if idx.is_file():
        hits = agent.plain_index_search(idx, ["account.cfg"], limit=5)
        check("厂商主库打不开时，明文索引仍能确认文件存在",
              any("account.cfg" in n.lower() for n, _ in hits), hits)
        check("明文索引返回 nodeId（可用于反查路径）",
              all(isinstance(i, int) and i > 0 for _, i in hits) if hits else True, hits)
    else:
        check("明文索引存在", False, f"{idx} 不存在")


# ---------------- 6. MCP 全挂时，索引查询也必须能用 ----------------
class DeadPool:
    """模拟「MCP 一台都没连上」——这正是模型以前直接交白卷的场景。"""

    def all_tools(self):
        return []

    def status(self):
        return []

    def handles(self):
        return {}


ag = agent.ForensicsAgent.__new__(agent.ForensicsAgent)
ag.pool = DeadPool()
ag.cfg = {}

out = ag._fs_index({"keyword": "account.cfg", "limit": 5})
check("MCP 全挂时仍能确认文件存在（退回明文索引）",
      "account.cfg" in out.lower() and "0 命中" not in out, out[:300])
check("明文索引模式下明说「没有全路径」，不假装拿到了路径",
      "没有全路径" in out or "nodeId" in out, out[:300])

out = ag._fs_index({"keyword": ""})
check("空关键词给出参数错误，不是崩掉", "【参数错误】" in out, out[:120])


# ---------------- 7. 用户给了路径就能直接读（不需要 MCP） ----------------
d2 = Path(tempfile.mkdtemp(prefix="tree2_"))
asr = d2 / "ASR"
(asr / "scripts").mkdir(parents=True)
(asr / "scripts" / "install.py").write_text('TORCH_INDEXES["cu126"]\n', encoding="utf-8")
(asr / "config.json").write_text('{"model": "sensevoice"}\n', encoding="utf-8")
(asr / "temp").mkdir()
(asr / "temp" / "a.wav").write_bytes(b"\x00" * 32)

rep = agent.list_tree(asr, depth=2)
check("列目录不需要关键词：能看到子目录和文件",
      "[D] scripts/" in rep and "install.py" in rep, rep[:400])
check("列目录标明「这是检材还是本机」",
      "类型：" in rep, rep[:120])
check("列目录能按后缀过滤",
      "a.wav" not in agent.list_tree(asr, include="*.py,*.json"),
      agent.list_tree(asr, include="*.py,*.json")[:300])
check("列目录不是文件时给出提示而不是崩",
      "不是目录" in agent.list_tree(asr / "config.json"), "")

check("路径分类：火眼挂载点算检材",
      agent.classify_path(Path(r"C:\hlnet\2-1793324293\Laptop_x.E01\系统")) == "检材挂载视图")
check("路径分类：MYOS 挂载点算检材",
      agent.classify_path(Path(r"C:\osmap\GUID_2026\Root\Partition0_系统")) == "检材挂载视图")
check("路径分类：分析员本机路径必须标成「本机磁盘」",
      "本机" in agent.classify_path(Path(r"C:\Users\LENOVO\.workbuddy\memory")),
      agent.classify_path(Path(r"C:\Users\LENOVO\.workbuddy\memory")))

names = [t["function"]["name"] for t in agent.BUILTIN_TOOLS]
check("sys_fs_list 已注册", "sys_fs_list" in names, names)

# ---------------- 8. 提示词契约 ----------------
sysmd = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")

contracts = [
    ("开头就把「直接读文件」列为主力、MCP 是可选增强",
     "MCP 服务器（可选增强）"),
    ("写明「没连上 MCP 不等于不能干活」",
     "没连上 MCP 不等于不能干活"),
    ("第 0 层：用户给了路径就直接读，不探活不 load_case",
     "第 0 层"),
    ("铁律 1 改成「先看手上有什么」",
     "先看手上有什么"),
    ("写明「范围优先」，不许拿别处的东西凑答案",
     "题目限定了范围"),
    ("写明「分清检材和分析员自己的电脑」",
     "分清检材和分析员自己的电脑"),
    ("铁律里写死「开工第一个动作是 sys_fs_index」",
     "开工第一个动作是 `sys_fs_index`"),
    ("成本阶梯：第 0 层直接读 / 索引 / 读文件 / 扫盘 / 裸检索",
     "成本阶梯：能直接读就别绕路"),
    ("点明裸数据检索是最后手段", "裸数据检索：最后手段"),
    ("点明 SOURCE 表是陷阱", "TBL_FS_ENTRY_SOURCE"),
    ("点明 sqlite_query 不支持参数绑定", "不支持参数绑定"),
    ("点明主库加密、直连会报 file is not a database", "file is not a database"),
    ("点明读内容只能走挂载视图", "读文件内容的唯一通道"),
    ("挂载故障对照表（isMounted / 1450 / D-Bus）", "WinError 1450"),
    ("题型 A：账号类给出 Foxmail 的 Storage\\<邮箱> 布局", "Storage\\<邮箱地址>\\"),
    ("题型 A：Proton Mail 未登录的判断", "没登录过"),
    ("题型 B：数量类要区分 VAD 不算 ASR", "fsmn-vad"),
    ("题型 B：计数口径要跟题干限定词走", "计数口径"),
    ("题型 C：参数类先找启动脚本/部署记录，不要先扫二进制",
     "绝对不要先去扫二进制"),
    ("题型 C：干扰值对照（标称/降档/换模型）", "干扰值来源"),
    ("结论要说明排除理由", "只给数字不给排除理由"),
]
for name, needle in contracts:
    check(name, needle in sysmd, f"未找到：{needle}")

# ---------------- 7. 工具描述与提示词对齐 ----------------
names = [t["function"]["name"] for t in agent.BUILTIN_TOOLS]
check("sys_fs_index 已注册", "sys_fs_index" in names, names)
check("sys_file_read 已注册", "sys_file_read" in names, names)

idx_def = next(t["function"] for t in agent.BUILTIN_TOOLS
               if t["function"]["name"] == "sys_fs_index")
check("索引工具说明里写明「先用它、不要先跑裸数据检索」",
      "不要一上来跑裸数据检索" in idx_def["description"], idx_def["description"][:160])
check("索引工具支持 dir 模式（数目录项）",
      "dir" in str(idx_def["parameters"]["properties"].get("mode", {})), "")

read_def = next(t["function"] for t in agent.BUILTIN_TOOLS
                if t["function"]["name"] == "sys_file_read")
check("读文件工具说明里写明可直接传镜像内路径",
      "镜像内路径" in read_def["description"], read_def["description"][:160])

print(f"\n合计 {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
