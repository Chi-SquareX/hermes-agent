#!/usr/bin/env python3
"""Stop anything on the Hermes dashboard port and start uvicorn once."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = int(os.getenv("HERMES_PORT", "8010"))


def pids_on_port(port: int) -> set[str]:
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return set()
        pids: set[str] = set()
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
        return pids

    try:
        out = subprocess.check_output(["lsof", "-ti", f"tcp:{port}"], text=True).strip()
    except Exception:
        return set()
    return {pid for pid in out.splitlines() if pid.strip()}


def kill_port(port: int) -> None:
    for _ in range(5):
        pids = pids_on_port(port)
        if not pids:
            return
        for pid in pids:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.run(["kill", "-9", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)


def start_uvicorn(port: int, reload: bool) -> int:
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if reload:
        cmd.append("--reload")
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart Hermes dashboard on a fixed port.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args()

    print(f"Stopping processes on port {args.port}...")
    kill_port(args.port)
    remaining = pids_on_port(args.port)
    if remaining:
        print(
            f"Port {args.port} is still in use by PID(s): {', '.join(sorted(remaining))}. "
            "Close Cursor/Hermes MCP or run: docker compose down",
            file=sys.stderr,
        )
        return 1

    print(f"Starting dashboard at http://127.0.0.1:{args.port}")
    return start_uvicorn(args.port, reload=not args.no_reload)


if __name__ == "__main__":
    raise SystemExit(main())
