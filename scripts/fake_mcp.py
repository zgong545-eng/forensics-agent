"""一个最小的 MCP stdio 服务器，只提供一个 ping 工具。

专门用来测试「连接中途断开能不能自动重连」——进程可以被外部杀掉，
模拟取证软件 D-Bus 断开的场景。
"""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        meth = msg.get("method")
        if "id" not in msg:            # notification
            continue
        rid = msg["id"]
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "ping",
                "description": "返回 pong",
                "inputSchema": {"type": "object", "properties": {}},
            }]}})
        elif meth == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "pong"}],
            }})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})


if __name__ == "__main__":
    main()
