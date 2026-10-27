"""Narrow, reversible OpenSSH integration; private keys remain in OpenSSH."""

import datetime
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

MARKER = "# Managed by FudanGateway; generated from local private configuration."


def quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Newline in SSH configuration value.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(config: dict, executable: str) -> str:
    if not Path(executable).is_absolute():
        raise ValueError("The SSH helper executable must have an absolute path.")
    lines = [MARKER, ""]
    for name, target in config["ssh_targets"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid SSH target alias.")
        lines += [
            f"Host {name} {name}-webvpn",
            "    HostName " + quote(target["hostname"]),
            "    User " + quote(target["user"]),
            f"    Port {int(target['port'])}",
            "    IdentityFile " + quote(target["identity_file"]),
            "    IdentitiesOnly yes",
            "    HostKeyAlias " + quote(target["host_key_alias"]),
            "    UserKnownHostsFile " + quote(target["known_hosts_file"]),
            "    GlobalKnownHostsFile /dev/null",
            "    StrictHostKeyChecking yes",
            "    UpdateHostKeys no",
            "    ForwardAgent no",
            "    ControlMaster no",
            "    ControlPath none",
            "    ServerAliveInterval 20",
            "    ServerAliveCountMax 3",
            "    ConnectTimeout 30",
            "    ProxyCommand " + quote(executable) + " connect " + name,
            "",
        ]
    return "\n".join(lines)


def install(config: dict, root: Path, executable: str, ssh_dir: Path | None = None):
    ssh_dir = ssh_dir or Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    managed = ssh_dir / "fudan-gateway.conf"
    if managed.exists() and not managed.read_text().startswith(MARKER):
        raise ValueError("Existing SSH include is not managed by FudanGateway.")
    original = ssh_dir / "config"
    text = original.read_text() if original.exists() else ""
    include = "Include " + quote(str(managed)) + " # fudan-gateway\n"
    backup = None
    if include not in text:
        backup_dir = root / "backups"
        backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = backup_dir / f"ssh-config-{stamp}"
        backup.write_text(text)
        backup.chmod(0o600)
    managed.write_text(render(config, executable))
    managed.chmod(0o600)
    if include not in text:
        original.write_text(include + text)
        original.chmod(0o600)
    return {"aliases": list(config["ssh_targets"]), "backup_created": backup is not None}


def install_agent(root: Path, executable: str):
    if shutil.which("launchctl") is None:
        raise ValueError("LaunchAgent installation is supported on macOS.")
    label = "com.biogod.fudan-gateway.broker"
    target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": label,
        "ProgramArguments": [executable, "broker"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "Umask": 63,
        "EnvironmentVariables": {"FUDANGATEWAY_STATE_DIR": str(root)},
        "StandardOutPath": str(root / "launchd.log"),
        "StandardErrorPath": str(root / "launchd.log"),
    }
    if target.exists():
        existing = plistlib.loads(target.read_bytes())
        if existing.get("Label") != label:
            raise ValueError("Unexpected existing LaunchAgent.")
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, check=False
        )
    target.write_bytes(plistlib.dumps(data))
    target.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(target)], capture_output=True, check=True)
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{label}"], check=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
    return label
