"""Prepare the isolated aTrust runtime; start services only with --activate."""

import argparse
import datetime
import json
import os
import plistlib
import shutil
import socket
import subprocess
from pathlib import Path

ATRUST = Path(__file__).resolve().parents[1]
LABEL = "com.biogod.fudan-gateway.atrust"
MENU = LABEL + ".menu"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=ATRUST.parent / "dist/atrust")
    parser.add_argument(
        "--runtime",
        type=Path,
        default=Path.home() / "Library/Application Support/FudanGateway/atrust",
    )
    parser.add_argument(
        "--app", type=Path, default=Path.home() / "Applications/FudanGateway aTrust.app"
    )
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root, app = args.runtime.expanduser().resolve(), args.app.expanduser().resolve()
    if args.dry_run:
        print(json.dumps({"runtime": str(root), "app": str(app), "activate": args.activate}))
        return
    if os.uname().sysname != "Darwin":
        parser.error("The login window and menu require macOS.")
    # Replacing files underneath a running instance would not replace that process.
    # Preserve active sessions and require the owner to stop this service first.
    domain = f"gui/{os.getuid()}"
    for label in (LABEL, MENU):
        current = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"], capture_output=True, check=False
        )
        if current.returncode == 0:
            parser.error(f"Stop the existing {label} job before replacing its files.")
    if args.activate:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 1080)) == 0:
                parser.error("Port 1080 is already in use; stop its owner before activation.")
    for name in ("fudan-supervisor", "fudan-login", "zju-connect"):
        if not (args.build_root / name).is_file():
            parser.error(f"Build output missing: {name}")
    source_app = args.build_root / "FudanGateway aTrust.app"
    if not source_app.is_dir():
        parser.error("Build the aTrust menu app first.")
    if app.exists():
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != MENU:
            parser.error("Destination app belongs to another implementation.")
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    (root / "logs").mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = root / "backups" / stamp
    if (root / "bin").exists():
        backup.mkdir(mode=0o700, parents=True)
        shutil.copytree(root / "bin", backup / "bin")
    if app.exists():
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copytree(app, backup / app.name, symlinks=True)
    (root / "bin").mkdir(mode=0o700, exist_ok=True)
    for name in ("fudan-supervisor", "fudan-login", "zju-connect"):
        shutil.copy2(args.build_root / name, root / "bin" / name)
        (root / "bin" / name).chmod(0o700)
    shutil.copy2(ATRUST / "scripts/fudan-gateway.command", root / "bin/fudan-gateway.command")
    (root / "bin/fudan-gateway.command").chmod(0o700)
    for name in ("config.toml", "health.json"):
        if not (root / name).exists():
            shutil.copyfile(ATRUST / "examples" / name, root / name)
        (root / name).chmod(0o600)
    app.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_app, app, symlinks=True, dirs_exist_ok=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", str(app)], check=True)
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label, executable in (
        (LABEL, root / "bin/fudan-supervisor"),
        (MENU, app / "Contents/MacOS/FudanGateway"),
    ):
        payload = {
            "Label": label,
            "ProgramArguments": [str(executable)],
            "WorkingDirectory": str(root),
            "EnvironmentVariables": {"FUDANGATEWAY_ATRUST_HOME": str(root)},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 300 if label == LABEL else 60,
            "ProcessType": "Background" if label == LABEL else "Interactive",
            "Umask": 63,
            "StandardOutPath": str(root / "logs" / (label + ".log")),
            "StandardErrorPath": str(root / "logs" / (label + ".log")),
        }
        target = agents / f"{label}.plist"
        if target.exists():
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(target, backup / target.name)
        target.write_bytes(plistlib.dumps(payload))
        target.chmod(0o600)
        subprocess.run(["plutil", "-lint", str(target)], capture_output=True, check=True)
        if args.activate:
            subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=True)
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(json.dumps({"runtime": str(root), "app": str(app), "activated": args.activate}))


if __name__ == "__main__":
    main()
