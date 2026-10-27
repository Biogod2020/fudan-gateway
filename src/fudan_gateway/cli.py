"""Connect stdout is reserved exclusively for native SSH bytes."""

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .storage import read_private, runtime_dir, write_private


def parser():
    p = argparse.ArgumentParser(prog="fudangateway")
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    relay = commands.add_parser("relay", help="Run the authorized campus relay")
    relay.add_argument("--config", type=Path, required=True)
    for name in ("connect", "probe"):
        commands.add_parser(name).add_argument("target")
    login = commands.add_parser("login", help="Open the official UIS login")
    login.add_argument(
        "--credentials-stdin", action="store_true", help="Read one-time JSON from stdin"
    )
    commands.add_parser("broker", help="Run the private login assistant")
    commands.add_parser(
        "stop", help="Stop the login assistant; active SSH sessions are independent"
    )
    commands.add_parser("status")
    commands.add_parser(
        "diagnostics", help="Show safe browser stages without cookies or URL queries"
    )
    commands.add_parser("mode")
    commands.add_parser("open").add_argument("url", nargs="?")
    get = commands.add_parser("get")
    get.add_argument("url")
    get.add_argument("--output", type=Path)
    commands.add_parser("doctor").add_argument("--native-ssh", metavar="ALIAS")
    install = commands.add_parser("install-client")
    install.add_argument("--launch-agent", action="store_true")
    install.add_argument("--executable", type=Path)
    commands.add_parser("keygen").add_argument("output", type=Path)
    return p


def emit(data):
    print(json.dumps(data, ensure_ascii=False))


async def run_client(args):
    from .control import ensure_session, request
    from .session import NeedsLogin
    from .transport import connect_stdio, probe_banner

    root = runtime_dir()
    if args.command == "broker":
        from .broker import serve

        await serve(root)
        return
    if args.command == "stop":
        emit(await request(root, "stop"))
        return
    if args.command == "diagnostics":
        emit(await request(root, "diagnostics"))
        return
    if args.command == "status":
        path = root / "status.json"
        data = read_private(path) if path.exists() else {"state": "unverified"}
        data["mode"] = "webvpn"
        if checked := data.get("checked_at"):
            data["checked_age_seconds"] = round(time.time() - checked)
            if data["checked_age_seconds"] > 300 and data["state"] == "ready":
                data["state"] = "stale"
        emit(data)
        return
    config = read_private(root / "config.json")
    if config.get("mode") != "webvpn":
        raise ValueError("Select mode webvpn in the local gateway configuration.")
    if args.command == "mode":
        emit({"mode": "webvpn", "native_ssh": "authenticated-campus-relay"})
        return
    if args.command == "login":
        credentials = None
        if args.credentials_stdin:
            credentials = json.loads(sys.stdin.read(16384))
            if not all(isinstance(credentials.get(k), str) for k in ("username", "password")):
                raise ValueError("Expected username/password JSON on stdin.")
        try:
            emit(await request(root, "login", timeout=210, credentials=credentials, force=True))
        finally:
            if credentials:
                credentials.clear()
        return
    if args.command == "open":
        emit(await request(root, "open", timeout=210, url=args.url))
        return
    if args.command == "install-client":
        from .sshconfig import install, install_agent

        executable = str(
            args.executable or Path(shutil.which("fudangateway") or sys.argv[0]).resolve()
        )
        result = install(config, root, executable)
        if args.launch_agent:
            result["launch_agent"] = install_agent(root, executable)
        emit(result)
        return

    session = await ensure_session(root)
    for attempt in range(2):
        try:
            if args.command == "connect":
                await connect_stdio(session, config["relay"], args.target)
            elif args.command == "probe":
                emit(
                    {
                        "target": args.target,
                        "ssh_banner": await probe_banner(session, config["relay"], args.target),
                    }
                )
            elif args.command == "get":
                code, _, body = await session.get(args.url)
                if args.output:
                    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(body)
                title = re.search(rb"<title[^>]*>(.*?)</title>", body, re.S | re.I)
                emit(
                    {
                        "http": code,
                        "bytes": len(body),
                        "title": title.group(1).decode("utf-8", "replace").strip()
                        if title
                        else None,
                    }
                )
            elif args.command == "doctor":
                code, _, body = await session.get(
                    config.get("health_resource", "https://library.fudan.edu.cn/")
                )
                if code != 200 or "复旦大学图书馆" not in body.decode("utf-8", "replace"):
                    raise RuntimeError("Authenticated resource health check failed.")
                checks = {}
                for target in config["relay"]["targets"]:
                    checks[target] = await probe_banner(session, config["relay"], target)
                result = {
                    "state": "ready",
                    "mode": "webvpn",
                    "checked_at": time.time(),
                    "resource_http": code,
                    "ssh_banners": checks,
                }
                if args.native_ssh:
                    if args.native_ssh not in config["ssh_targets"]:
                        raise ValueError("Native SSH alias is not managed by this configuration.")
                    process = await asyncio.create_subprocess_exec(
                        "/usr/bin/ssh",
                        "-S",
                        "none",
                        "-o",
                        "ControlMaster=no",
                        "-o",
                        "BatchMode=yes",
                        args.native_ssh,
                        "printf FUDANGATEWAY_SSH_OK",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(process.communicate(), 45)
                    if process.returncode or stdout != b"FUDANGATEWAY_SSH_OK":
                        raise RuntimeError("Native SSH authentication did not pass.")
                    result["native_ssh_verified"] = args.native_ssh
                write_private(root / "status.json", result)
                emit(result)
            return
        except NeedsLogin:
            if attempt:
                raise
            session = await ensure_session(root, force=True)


def main():
    args = parser().parse_args()
    try:
        if args.command == "relay":
            from .relay import run

            run(args.config)
        elif args.command == "keygen":
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_hex(32) + "\n")
            emit({"key_file_created": str(args.output)})
        else:
            asyncio.run(run_client(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        from .session import GatewayError, NeedsLogin

        if isinstance(error, NeedsLogin):
            print("fudangateway: UIS login required; run fudangateway login.", file=sys.stderr)
            raise SystemExit(3) from None
        safe = str(error) if isinstance(error, (ValueError, GatewayError)) else type(error).__name__
        print("fudangateway: " + safe, file=sys.stderr)
        raise SystemExit(1) from None
