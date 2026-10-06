#!/usr/bin/env python3
"""Pi ACP Bridge — exposes pi as an ACP server for OpenHands ACPAgent.

Reads ACP JSON-RPC messages on stdin, delegates prompts to
``pi --mode rpc``, and writes ACP responses to stdout.

Environment:
    PI_CWD      Working directory for pi (default: current dir)
    PI_MODEL    Model override (default: pi auto-select)
    PI_DEBUG    Set to 1 for verbose logging to stderr
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ACP_PROTOCOL_VERSION = "2025-03-26"
DEBUG = os.environ.get("PI_DEBUG", "0") == "1"


def _log(msg: str) -> None:
    if DEBUG:
        print(f"[pi-acp-bridge] {msg}", file=sys.stderr, flush=True)


class PiRpcClient:
    """JSON-RPC client for ``pi --mode rpc`` over stdio."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd or os.getcwd()
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        cmd = ["pi", "--mode", "rpc", "--no-session"]
        if model := os.environ.get("PI_MODEL"):
            cmd += ["--model", model]
        _log(f"starting: {' '.join(cmd)} (cwd={self.cwd})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        _log(f"pi started (pid={self._proc.pid})")

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while not self._closed:
            try:
                line = await self._proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            fut = self._pending.get(msg_id) if msg_id is not None else None
            if fut and not fut.done():
                fut.set_result(msg)

    async def call(self, method: str, params: dict | None = None) -> dict:
        async with self._lock:
            self._req_id += 1
            req_id = self._req_id
            req = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = fut
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(json.dumps(req).encode() + b"\n")
            await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=300.0)
        finally:
            self._pending.pop(req_id, None)

    async def prompt(self, text: str) -> str:
        result = await self.call("prompt", {"text": text})
        if "error" in result:
            raise RuntimeError(result["error"])
        payload = result.get("result", {})
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            for key in ("text", "content", "response", "output", "message"):
                if key in payload:
                    val = payload[key]
                    return val if isinstance(val, str) else json.dumps(val)
        return json.dumps(payload)

    async def close(self) -> None:
        self._closed = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc:
            with asyncio.suppress(ProcessLookupError):
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)


class PiAcpServer:
    """Minimal ACP server delegating prompts to pi."""

    def __init__(self) -> None:
        self.pi = PiRpcClient(cwd=os.environ.get("PI_CWD"))

    async def run(self) -> None:
        await self.pi.start()
        self._send(
            {
                "type": "server_info",
                "protocol_version": ACP_PROTOCOL_VERSION,
                "server_name": "pi-acp-bridge",
                "server_version": "0.1.0",
            }
        )
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                await self._handle(json.loads(line.decode()))
            except json.JSONDecodeError:
                _log("invalid ACP JSON, skipping")
            except Exception as exc:
                _log(f"handler error: {exc}")
        await self.pi.close()

    async def _handle(self, msg: dict) -> None:
        msg_type = msg.get("type")
        req_id = msg.get("id")
        _log(f"ACP request: {msg_type} id={req_id}")

        if msg_type == "initialize":
            self._send(
                {
                    "type": "initialize_response",
                    "id": req_id,
                    "protocol_version": ACP_PROTOCOL_VERSION,
                    "server_info": {"name": "pi-acp-bridge", "version": "0.1.0"},
                    "capabilities": {},
                }
            )
        elif msg_type == "session/new":
            self._send(
                {
                    "type": "session/new_response",
                    "id": req_id,
                    "session_id": "pi-ephemeral",
                }
            )
        elif msg_type == "prompt":
            try:
                text = await self.pi.prompt(msg.get("text", ""))
                self._send(
                    {
                        "type": "prompt_response",
                        "id": req_id,
                        "content": [{"type": "text", "text": text}],
                    }
                )
            except Exception as exc:
                self._send(
                    {
                        "type": "error",
                        "id": req_id,
                        "code": -32000,
                        "message": str(exc),
                    }
                )
        elif msg_type == "ping":
            self._send({"type": "pong", "id": req_id})
        else:
            _log(f"unhandled ACP type: {msg_type}")
            self._send(
                {
                    "type": "error",
                    "id": req_id,
                    "code": -32601,
                    "message": f"Method not found: {msg_type}",
                }
            )

    def _send(self, msg: dict) -> None:
        print(json.dumps(msg, ensure_ascii=False), flush=True)


def main() -> None:
    if shutil.which("pi") is None:
        print("ERROR: 'pi' not found on PATH.", file=sys.stderr)
        sys.exit(1)
    log_path = Path(tempfile.gettempdir()) / "pi-acp-bridge.log"
    _log(f"logging to {log_path}")
    try:
        asyncio.run(PiAcpServer().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
