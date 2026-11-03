#!/usr/bin/env python3
"""A protocol-faithful stand-in for ``a-coder-cli --mode rpc``.

Tests must never spend LLM tokens or depend on provider auth, so the bridge is
exercised against this child instead. It implements exactly the frames the
engine uses:

* the ``engine_info`` handshake
* ``get_state`` / ``get_messages`` (for late-joiner replay)
* ``prompt`` -> accepted response, then a streamed assistant turn
* ``abort``, ``set_model``, ``get_available_models``
* unknown commands -> ``success: false``

Extra flags make failure modes reproducible:

``--burst N``      emit N message_update frames for one prompt (backpressure)
``--crash-after N``      exit(1) after N frames read (supervision/restart)
``--crash-immediately``  exit(1) right after the handshake (restart budget)
``--no-handshake``       never emit engine_info (start-timeout path)
``--stall``              handshake, then never answer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

CONTRACT = 1


class FakeEngine:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.seq = 0
        self.frames_in = 0
        self.messages: list[dict] = []
        self.session_file = os.path.join(args.session_dir, f"fake-{os.getpid()}.jsonl")
        self.prompt_count = 0

    def log_command(self, cmd: dict) -> None:
        """Append every received command so tests can assert bootstrap policy."""
        if not self.args.command_log:
            return
        with open(self.args.command_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), **cmd}) + "\n")

    # -- output ------------------------------------------------------------

    def emit(self, frame: dict) -> None:
        self.seq += 1
        frame.setdefault("seq", self.seq)
        frame.setdefault("sessionId", "fake-session")
        sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def respond(self, cmd: dict, data: dict | None = None) -> None:
        out: dict = {"type": "response", "command": cmd.get("type"), "success": True}
        if "id" in cmd:
            out["id"] = cmd["id"]
        if data is not None:
            out["data"] = data
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    def fail(self, cmd: dict, message: str, code: str = "unsupported") -> None:
        out: dict = {"type": "response", "command": cmd.get("type"), "success": False, "error": message, "code": code}
        if "id" in cmd:
            out["id"] = cmd["id"]
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    # -- handlers ----------------------------------------------------------

    def state(self) -> dict:
        return {
            "model": {"provider": "fake", "id": "fake-1", "name": "Fake", "contextWindow": 262144},
            "thinkingLevel": "off",
            "permissionMode": "allow",
            "planMode": False,
            "isStreaming": False,
            "isCompacting": False,
            "steeringMode": "all",
            "followUpMode": "one-at-a-time",
            "cwd": os.getcwd(),
            "sessionFile": self.session_file,
            "sessionId": "fake-session",
            "activationId": 1,
            "autoCompactionEnabled": True,
            "compactionAutoContinue": True,
            "messageCount": len(self.messages),
            "pendingMessageCount": 0,
        }

    def handle(self, cmd: dict) -> None:
        ctype = cmd.get("type")
        if ctype == "get_state":
            self.respond(cmd, self.state())
        elif ctype == "get_session_snapshot":
            self.respond(
                cmd,
                {
                    "state": self.state(),
                    "stats": {
                        "sessionId": "fake-session",
                        "messageCount": len(self.messages),
                        "contextUsage": {"tokens": 42, "contextWindow": 262144},
                    },
                },
            )
        elif ctype == "get_messages":
            limit = cmd.get("limit")
            before = cmd.get("before", len(self.messages))
            start = 0 if limit is None else max(0, before - int(limit))
            page = self.messages[start:before]
            self.respond(
                cmd,
                {
                    "messages": page,
                    "total": len(self.messages),
                    "start": start,
                    "end": start + len(page),
                    "hasMore": start > 0,
                },
            )
        elif ctype == "get_available_models":
            self.respond(cmd, {"models": [{"provider": "fake", "id": "fake-1", "contextWindow": 262144}]})
        elif ctype == "set_model":
            self.respond(cmd, {"provider": cmd.get("provider"), "id": cmd.get("modelId")})
        elif ctype == "abort":
            self.respond(cmd)
        elif ctype in ("set_auto_compaction", "set_auto_retry"):
            if self.args.fail_policy:
                self.fail(cmd, f"fake engine does not implement {ctype}")
            else:
                self.respond(cmd)
        elif ctype == "prompt":
            self.respond(cmd)
            self.run_turn(str(cmd.get("message", "")))
        elif ctype == "extension_ui_response":
            pass
        else:
            self.fail(cmd, f"fake engine does not implement {ctype}")

    def run_turn(self, text: str) -> None:
        self.prompt_count += 1
        self.emit({"type": "agent_start"})
        reply = f"echo: {text}"
        self.messages.append({"role": "user", "content": text})
        self.emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
        for i, word in enumerate(reply.split(" ")):
            self.emit(
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "contentIndex": 0,
                        "delta": word + (" " if i < len(reply.split(" ")) - 1 else ""),
                    },
                }
            )
        for _ in range(max(0, self.args.burst)):
            self.emit({"type": "message_update", "message": {"role": "assistant", "content": []}})
        self.messages.append({"role": "assistant", "content": reply})
        self.emit({"type": "message_end", "message": {"role": "assistant", "content": reply}})
        self.emit({"type": "agent_end"})

    # -- loop --------------------------------------------------------------

    def run(self) -> int:
        if self.args.no_handshake:
            # Worst case for the bridge: a child that starts but never speaks.
            time.sleep(3600)
            return 0
        self.emit(
            {
                "type": "engine_info",
                "version": self.args.version,
                "contract": CONTRACT,
                "capabilities": ["delta", "batch", "tool-progress", "attachments"],
            }
        )
        if self.args.crash_immediately:
            return 1
        if self.args.stall:
            time.sleep(3600)
            return 0
        buffer = b""
        while True:
            # os.read returns as soon as any bytes are available. A plain
            # BufferedReader.read(n) would block until n bytes or EOF, which
            # stalls a command/reply protocol.
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while True:
                idx = buffer.find(b"\n")
                if idx == -1:
                    break
                raw, buffer = buffer[:idx], buffer[idx + 1 :]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if not raw.strip():
                    continue
                self.frames_in += 1
                if self.args.crash_after and self.frames_in >= self.args.crash_after:
                    sys.stderr.write("fake engine crashing on purpose\n")
                    sys.stderr.flush()
                    return 1
                cmd = json.loads(raw.decode("utf-8"))
                self.log_command(cmd)
                self.handle(cmd)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="rpc")
    parser.add_argument("--session-dir", default="/tmp")
    parser.add_argument("--continue", dest="cont", action="store_true")
    parser.add_argument("--burst", type=int, default=0)
    parser.add_argument("--crash-after", type=int, default=0)
    parser.add_argument("--stall", action="store_true")
    parser.add_argument("--no-handshake", action="store_true")
    parser.add_argument("--crash-immediately", action="store_true")
    parser.add_argument("--command-log", default=None, metavar="PATH")
    parser.add_argument("--fail-policy", action="store_true")
    parser.add_argument("--version", dest="version", default="fake-0.0.0")
    args, _unknown = parser.parse_known_args(argv)
    os.makedirs(args.session_dir, exist_ok=True)
    return FakeEngine(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
