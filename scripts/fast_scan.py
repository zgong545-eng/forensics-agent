"""分片 + 多线程扫描超大本地文件（只读，不写任何东西）。

为什么要这个：dokan 挂载单线程读约 20MB/s，18.5GB 的 ext4.vhdx 单线程要 15 分钟。
分片让多个线程各读一段，能把 I/O 队列吃满，通常快 5~8 倍。

用法：
  python scripts/fast_scan.py <文件> <关键词1,关键词2> [--range 起:止] [--threads 8]
                                                 [--max-hits 40] [--regex 正则]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

CHUNK = 8 * 1024 * 1024
OVERLAP = 4096


def printable(b: bytes) -> str:
    wide = b.count(b"\x00") > len(b) // 4
    if wide:
        try:
            s = b.decode("utf-16-le", "ignore")
        except Exception:
            s = b.decode("latin-1", "ignore")
    else:
        s = b.decode("utf-8", "ignore")
    return re.sub(r"[\x00-\x1f]+", " ", s).strip()


def scan_range(path: str, start: int, end: int, pats, rx, hits, lock, max_hits,
               ignore_case=True):
    """扫 [start, end)。命中写进 hits（加锁），超过 max_hits 就只计数不再存。"""
    n = 0
    with open(path, "rb", buffering=0) as fh:
        fh.seek(start)
        pos = start
        tail = b""
        while pos < end:
            block = fh.read(min(CHUNK, end - pos))
            if not block:
                break
            buf = tail + block if tail else block
            base = pos - len(tail)
            pos += len(block)
            low = buf.lower() if ignore_case else buf
            for kw, pat, wide in pats:
                i = 0
                while True:
                    i = low.find(pat, i)
                    if i < 0:
                        break
                    n += 1
                    with lock:
                        if len(hits) < max_hits:
                            off = base + i
                            pad = 220 if wide else 160
                            w0 = max(0, i - pad)
                            w1 = min(len(buf), i + len(pat) + pad)
                            hits.append((off, kw, printable(buf[w0:w1])))
                    i += 1
            if rx is not None:
                for m in rx.finditer(buf):
                    n += 1
                    with lock:
                        if len(hits) < max_hits:
                            off = base + m.start()
                            w0 = max(0, m.start() - 160)
                            w1 = min(len(buf), m.end() + 160)
                            hits.append((off, "REGEX:" + m.group(0).decode("utf-8", "ignore"),
                                         printable(buf[w0:w1])))
            tail = buf[-OVERLAP:]
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("keywords", nargs="?", default="")
    ap.add_argument("--range", default="")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--max-hits", type=int, default=40)
    ap.add_argument("--regex", default="")
    ap.add_argument("--shard-mb", type=int, default=256)
    a = ap.parse_args()

    size = os.path.getsize(a.path)
    if a.range:
        lo, hi = a.range.split(":")
        start, end = int(lo or 0), min(size, int(hi or size))
    else:
        start, end = 0, size
    total = end - start

    kws = [k for k in a.keywords.split(",") if k]
    pats = []
    for kw in kws:
        for wide in (False, True):
            b = kw.encode("utf-16-le") if wide else kw.encode("utf-8", "replace")
            pats.append((kw, b.lower(), wide))
    rx = None
    if a.regex:
        rx = re.compile(a.regex.encode("utf-8", "replace"), re.I)
    if not pats and rx is None:
        print("没有关键词也没有正则。")
        return 2

    print(f"文件 {a.path}")
    print(f"区间 {start:,} ~ {end:,}（{(total/1073741824):.2f} GiB），"
          f"{a.threads} 线程 × {a.shard_mb} MiB 分片", flush=True)

    import threading
    lock = threading.Lock()
    hits: list[tuple[int, str, str]] = []
    spans = []
    step = a.shard_mb * 1024 * 1024
    p = start
    while p < end:
        spans.append((p, min(end, p + step)))
        p += step

    t0 = time.time()
    count = 0
    done = 0
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        futs = [ex.submit(scan_range, a.path, s, e, pats, rx, hits, lock,
                          a.max_hits, True) for s, e in spans]
        for f in futs:
            count += f.result()
            done += 1
            if done % 4 == 0 or done == len(futs):
                el = time.time() - t0
                mbps = (done / len(futs)) * total / 1048576 / max(el, 1e-6)
                print(f"  进度 {done}/{len(futs)} 片 · {el:.0f}s · "
                      f"约 {mbps:.0f} MB/s · 累计命中 {count}", flush=True)

    el = time.time() - t0
    print(f"\n扫完 {total:,} 字节，用时 {el:.1f}s（{total/1048576/el:.0f} MB/s），"
          f"命中 {count} 次（列出 {len(hits)} 条）")
    for off, kw, ctx in sorted(hits):
        print(f"\n[{off:,}] · {kw}\n    …{ctx}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
