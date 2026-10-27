import stat

import pytest

from fudan_gateway.sshconfig import install, quote, render


def config(tmp_path):
    return {
        "ssh_targets": {
            "campus": {
                "hostname": "host.example.invalid",
                "user": "alice",
                "port": 2222,
                "identity_file": str(tmp_path / "private key"),
                "host_key_alias": "campus-pinned",
                "known_hosts_file": str(tmp_path / "known hosts"),
            }
        }
    }


def test_install_preserves_existing_hosts_is_idempotent_and_backs_up(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    original = "Host unrelated\n    HostName other.example.invalid\n"
    (ssh / "config").write_text(original)
    runtime = tmp_path / "runtime"
    c = config(tmp_path)
    result = install(c, runtime, "/Applications/My Tool/bin/fg", ssh)
    assert result["backup_created"]
    assert next((runtime / "backups").iterdir()).read_text() == original
    assert (ssh / "config").read_text().endswith(original)
    assert not install(c, runtime, "/Applications/My Tool/bin/fg", ssh)["backup_created"]
    assert (ssh / "config").read_text().count("# fudan-gateway") == 1
    text = (ssh / "fudan-gateway.conf").read_text()
    assert 'ProxyCommand "/Applications/My Tool/bin/fg" connect campus' in text
    assert "StrictHostKeyChecking yes" in text
    assert stat.S_IMODE((ssh / "fudan-gateway.conf").stat().st_mode) == 0o600


def test_rejects_unmanaged_include_and_config_injection(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "fudan-gateway.conf").write_text("User-owned content")
    with pytest.raises(ValueError, match="not managed"):
        install(config(tmp_path), tmp_path / "runtime", "/bin/fg", ssh)
    with pytest.raises(ValueError, match="Newline"):
        quote("value\nProxyCommand unsafe")
    c = config(tmp_path)
    c["ssh_targets"]["bad *"] = c["ssh_targets"].pop("campus")
    with pytest.raises(ValueError, match="alias"):
        render(c, "/bin/fg")
