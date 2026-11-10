"""真实链路验证：起服务 → 发一个会长跑的问题 → 中途中断 / 插话 → 看 SSE 反应。

用法：python scripts/test_live_control.py
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

PORT = 8823
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def post(path, body, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())


def stream_chat(text, on_event, stop_after=None, inject_after=None):
    """发一条对话，边收 SSE 边回调；到点在另一个线程里打中断/插话。"""
    req = urllib.request.Request(
        BASE + "/api/chat", data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    events = []

    def fire():
        time.sleep(stop_after)
        try:
            print(f"  [t={stop_after}s] 发送中断 …")
            print("   stop ->", post("/api/chat/stop", {}).strip())
        except Exception as e:
            print("   stop 失败:", e)

    if stop_after:
        threading.Thread(target=fire, daemon=True).start()

    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            events.append(ev)
            on_event(ev, time.time() - t0)
    return events


def main():
    srv = subprocess.Popen([PY, "webui.py", "--port", str(PORT), "--no-browser"],
                           cwd=str(ROOT), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        for i in range(60):
            try:
                get("/api/status")
                break
            except Exception:
                time.sleep(1)
        st = get("/api/status")
        print(f"[服务] 端口 {PORT} 就绪 · MCP {st.get('connected')} · "
              f"模型 {st.get('model')} · running={st.get('running')}")

        print("\n=== 场景 1：中途中断 ===")
        marks = []

        def on_ev(ev, t):
            if ev.get("type") in ("turn", "tool_start", "tool_end", "stopped", "final", "error"):
                marks.append((round(t, 1), ev.get("type"),
                              (ev.get("name") or ev.get("text") or "")[:40]))

        evs = stream_chat(
            "列出本机所有案件，逐个查每个案件的检材和取证状态，然后再逐个看证据数量",
            on_ev, stop_after=12)

        stopped = [e for e in evs if e.get("type") == "stopped"]
        finals = [e for e in evs if e.get("type") == "final"]
        print("  事件时间线：")
        for t, kind, txt in marks[:18]:
            print(f"    {t:>5.1f}s  {kind:<12} {txt}")
        print(f"  结果：stopped={len(stopped)} final={len(finals)} "
              f"共 {len(evs)} 个事件")
        if stopped:
            print(f"  ✓ 中断生效：{stopped[0].get('text', '')[:70]}")
        else:
            print("  ✗ 没收到 stopped 事件（可能已经跑完，或中断没接上）")

        time.sleep(1)
        print("  中断后状态：", get("/api/chat/state"))

        print("\n=== 场景 2：中途插话 ===")
        post("/api/reset", {})       # 干净上下文，否则上一场的中断记录会带进来
        time.sleep(1)
        injected = {"sent": False}

        def on_ev2(ev, t):
            if ev.get("type") == "inject":
                injected["sent"] = True
                print(f"  ✓ 模型收到了插话（t={t:.1f}s）：{ev.get('text', '')[:50]}")

        # 插话在第 6 秒打进去
        def do_inject():
            time.sleep(6)
            try:
                print("   inject ->", post("/api/chat/inject",
                                           {"text": "不用继续了，直接把目前已查到的结论讲一下"}).strip())
            except Exception as e:
                print("   inject 失败:", e)

        threading.Thread(target=do_inject, daemon=True).start()
        evs2 = stream_chat("列出本机所有案件，逐个查检材、取证状态和证据数量，越细越好",
                           on_ev2)
        print(f"  结果：inject 事件 {sum(1 for e in evs2 if e.get('type') == 'inject')} 个 · "
              f"共 {len(evs2)} 个事件")
        last = [e for e in evs2 if e.get("type") in ("final", "stopped")]
        if last:
            print(f"  收尾：{str(last[0].get('text'))[:100]}")

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()


if __name__ == "__main__":
    main()
