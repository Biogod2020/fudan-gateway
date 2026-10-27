"""Install only the separate WebVPN menu app; leave the legacy aTrust app alone."""

import argparse
import datetime
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from fudan_gateway.storage import runtime_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    root = runtime_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = Path("/Applications/FudanGateway WebVPN.app")
    label = "com.biogod.fudan-gateway.menu"
    agent = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if destination.exists():
        old = plistlib.loads((destination / "Contents/Info.plist").read_bytes())
        if old.get("CFBundleIdentifier") != label:
            raise ValueError("Existing destination belongs to a different application.")
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = root / "backups" / f"menu-{stamp}.app"
        backup.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copytree(destination, backup, symlinks=True)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, check=False
        )
    shutil.copytree(args.app, destination, dirs_exist_ok=True, symlinks=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(destination)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", str(destination)], check=True)
    payload = {
        "Label": label,
        "ProgramArguments": [str(destination / "Contents/MacOS/FudanGateway")],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "Umask": 63,
        "StandardOutPath": str(root / "menu.log"),
        "StandardErrorPath": str(root / "menu.log"),
    }
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(payload))
    agent.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(agent)], capture_output=True, check=True)
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{label}"], check=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agent)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
