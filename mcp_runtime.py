from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import mcp_manager


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, server: dict[str, Any]):
        self.server = server
        self.name = server.get("name") or server.get("id") or "mcp"
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.stderr: list[str] = []
        self._id = 0
        self._lock = threading.Lock()
        self._start()

    def _start(self) -> None:
        cmd = str(self.server.get("command") or "").strip()
        args = [str(x) for x in (self.server.get("args") or [])]
        if not cmd:
            raise MCPError(f"MCP server {self.name}: empty command")
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.server.get("env") or {}).items()})
        cwd = self.server.get("path") or None
        try:
            self.proc = subprocess.Popen(
                [cmd] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, cwd=cwd, env=env)
        except Exception as exc:
            raise MCPError(f"MCP server {self.name}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._initialize()

    def _read_stdout(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.q.put(line)
        self.q.put("")

    def _read_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            self.stderr.append(line.strip())
            if len(self.stderr) > 200:
                self.stderr = self.stderr[-200:]

    def _send(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError(f"MCP server {self.name}: not running")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None,
                 timeout: float = 120.0) -> Any:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    line = self.q.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") != rid:
                    continue
                if "error" in msg:
                    raise MCPError(str(msg["error"])[:1000])
                return msg.get("result")
        detail = "; ".join(self.stderr[-5:])
        raise MCPError(f"MCP {self.name} timeout on {method}" + (f": {detail}" if detail else ""))

    def _initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "synx-ai-robot", "version": "1.0"},
        }, timeout=180)
        try:
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except Exception:
            pass

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, timeout=60)
        return list((result or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None,
                  timeout: float = 600.0) -> Any:
        return self._request("tools/call", {"name": name, "arguments": arguments or {}},
                             timeout=timeout)

    def close(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


_CLIENTS: dict[str, MCPClient] = {}
_LOCK = threading.Lock()


def _server(name: str | None = None) -> dict[str, Any]:
    servers = [s for s in mcp_manager.load_servers() if s.get("enabled")]
    if name:
        srv = next((s for s in servers if name in (s.get("id"), s.get("name"))), None)
        if not srv:
            raise MCPError(f"enabled MCP server not found: {name}")
        return srv
    if not servers:
        raise MCPError("no enabled MCP servers")
    return servers[0]


def _client(server: dict[str, Any]) -> MCPClient:
    key = str(server.get("id") or server.get("name"))
    with _LOCK:
        cli = _CLIENTS.get(key)
        if cli and cli.proc and cli.proc.poll() is None:
            return cli
        cli = MCPClient(server)
        _CLIENTS[key] = cli
        return cli


def list_tools(server: str | None = None) -> dict[str, Any]:
    srv = _server(server)
    cli = _client(srv)
    tools = cli.list_tools()
    return {"server": srv.get("name"), "count": len(tools),
            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:300],
                       "input_schema": t.get("inputSchema") or {}} for t in tools]}


def call(server: str | None, tool: str, arguments: dict[str, Any] | None = None,
         timeout: float = 600.0) -> dict[str, Any]:
    srv = _server(server)
    cli = _client(srv)
    result = cli.call_tool(tool, arguments or {}, timeout=timeout)
    return {"server": srv.get("name"), "tool": tool, "result": result,
            "stderr": cli.stderr[-10:]}


def close_all() -> None:
    with _LOCK:
        for cli in _CLIENTS.values():
            cli.close()
        _CLIENTS.clear()
