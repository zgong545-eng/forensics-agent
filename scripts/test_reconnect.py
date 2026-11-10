"""验证「MCP 进程中途死掉 → 自动重连」是否真的生效。

这是「调用工具一直到轮数上限」的根因：取证软件 D-Bus 一断，
以前每次调用都秒回「通道已关闭」，模型收到就重试，20 轮很快烧完。

用 scripts/fake_mcp.py 当靶子，杀掉它的进程再调用，看能不能自愈。
用法：python scripts/test_reconnect.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import McpPool  # noqa: E402

PY = sys.executable
FAKE = str(ROOT / "scripts" / "fake_mcp.py")


def main():
    pool = McpPool({
        "fake": {"command": PY, "args": [FAKE], "enabled": True, "timeout": 20},
    }, default_timeout=20)
    pool.connect_all()

    h = pool.get("fake")
    print(f"1) 初次连接 state={h.state} ready={h.ready} 工具={[t.get('name') for t in h.tools]}")
    assert h.ready, "首次连接就失败，后面不用测了"

    print("2) 正常调 ping …", end=" ")
    r = pool.call("fake", "ping", {})
    print(f"返回 {r}")
    assert "pong" in str(r)

    proc = h.client.proc
    print(f"3) 杀掉 MCP 进程 pid={proc.pid}（模拟 D-Bus 断开）")
    proc.kill()
    proc.wait(timeout=10)
    time.sleep(0.6)          # 给读线程时间把 _closed 置起来

    print(f"4) 杀完之后 ready={h.ready}  closed={getattr(h.client, '_closed', None)}")
    stale_ok = not h.ready
    print(f"   状态是否已同步为不可用：{'是' if stale_ok else '否（这就是往死通道里写的根源）'}")

    print("5) 再调一次 ping，应该自动重连 …", end=" ")
    t0 = time.time()
    try:
        r2 = pool.call("fake", "ping", {})
        cost = time.time() - t0
        print(f"返回 {r2}（耗时 {cost * 1000:.0f} ms）")
        healed = "pong" in str(r2)
    except Exception as e:
        cost = time.time() - t0
        print(f"失败：{type(e).__name__}: {e}（耗时 {cost * 1000:.0f} ms）")
        healed = False

    print(f"6) 重连后 ready={h.ready} state={h.state}")
    pool.close()

    print("\n" + "=" * 54)
    ok = stale_ok and healed
    print(f"[{'通过' if ok else '失败'}] 状态同步={'OK' if stale_ok else 'FAIL'} · 自动重连={'OK' if healed else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
