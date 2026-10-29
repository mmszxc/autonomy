from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
INSTALL_ML = REPO / "tools" / "install_ml.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required for installer tests")


def _bash_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-lc", f"cygpath -u {shlex.quote(str(path))}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows bash does not provide cygpath")
    return result.stdout.strip()


FAKE_HERMES = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_LOG"
if [[ ${1:-} == profile && ${2:-} == show ]]; then
    [[ ${3:-} == "$FAKE_PROFILE" && -d "$FAKE_HERMES_HOME" ]] || exit 2
    printf 'Name: %s\nPath: %s\n' "$FAKE_PROFILE" "$FAKE_HERMES_HOME"
    exit 0
fi
if [[ ${1:-} == -p ]]; then
    shift 2
fi
if [[ ${1:-} == plugins && ${2:-} == enable ]]; then
    printf 'enabled.%s=true\n' "$3" >> "$FAKE_HERMES_HOME/config.yaml"
    exit 0
fi
if [[ ${1:-} == config && ${2:-} == set ]]; then
    printf '%s=%s\n' "$3" "$4" >> "$FAKE_HERMES_HOME/config.yaml"
    [[ -z ${FAKE_FAIL_KEY:-} || $3 != "$FAKE_FAIL_KEY" ]] || exit 9
    exit 0
fi
if [[ ${1:-} == plugins && ${2:-} == doctor ]]; then
    exit 0
fi
if [[ ${1:-} == autonomy && ${2:-} == doctor ]]; then
    exit 0
fi
printf 'unexpected fake hermes command: %s\n' "$*" >&2
exit 64
"""


FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
"""


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "demo"
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_text("provider_config: keep-me\n", encoding="utf-8")

    protected = {
        profile / "SOUL.md": b"identity must survive\n",
        profile / ".env": b"EXAMPLE_FLAG=keep\n",
        profile / "auth.json": b'{"example":"keep"}\n',
        profile / "seed_data" / "seeds" / "seeds.db": b"seed-db-sentinel",
        profile / "autonomy_data" / "state.db": b"state-db-sentinel",
    }
    for path, data in protected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = _executable(bin_dir / "hermes", FAKE_HERMES)
    systemctl = _executable(bin_dir / "systemctl", FAKE_SYSTEMCTL)
    fake_log = tmp_path / "hermes.log"
    systemctl_log = tmp_path / "systemctl.log"
    env = os.environ.copy()
    bash_bin = _bash_path(bin_dir)
    env.update(
        {
            "HOME": _bash_path(home),
            "XDG_CONFIG_HOME": _bash_path(home / ".config"),
            "HERMES_BIN": _bash_path(hermes),
            "SYSTEMCTL_BIN": _bash_path(systemctl),
            "FAKE_PROFILE": "demo",
            "FAKE_HERMES_HOME": _bash_path(profile),
            "FAKE_LOG": _bash_path(fake_log),
            "FAKE_SYSTEMCTL_LOG": _bash_path(systemctl_log),
            "PATH": f"{bash_bin}:/usr/local/bin:/usr/bin:/bin",
        }
    )
    return env, profile, config


def _run(env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, _bash_path(INSTALL), *args],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def test_dry_run_temp_home_installs_without_touching_protected_data(tmp_path: Path) -> None:
    env, profile, config = _fixture(tmp_path)
    original_config = config.read_bytes()
    protected = {
        path: path.read_bytes()
        for path in (
            profile / "SOUL.md",
            profile / ".env",
            profile / "auth.json",
            profile / "seed_data" / "seeds" / "seeds.db",
            profile / "autonomy_data" / "state.db",
        )
    }

    result = _run(env, "--profile", "demo", "--skip-ml", "--no-timer", "--yes")

    assert "installation complete" in result.stdout
    assert (profile / "plugins" / "autonomy" / "plugin.yaml").is_file()
    assert (profile / "plugins" / "seed_memory" / "seed_src" / "hermes" / "memory" / "store.py").is_file()
    for path, expected in protected.items():
        assert path.read_bytes() == expected
    assert b"provider_config: keep-me" in config.read_bytes()
    backups = list((profile / ".autonomy-install-backups").glob("*/config.yaml"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_config

    unit_dir = profile.parents[2] / ".config" / "systemd" / "user"
    service = unit_dir / "demo-autonomy.service"
    assert service.is_file()
    assert not (unit_dir / "demo-autonomy.timer").exists()
    service_text = service.read_text(encoding="utf-8")
    assert _bash_path(profile) in service_text
    assert "--hermes-home" in service_text
    systemctl_log = (profile.parents[3] / "systemctl.log").read_text(encoding="utf-8")
    assert "--user daemon-reload" in systemctl_log
    assert "enable --now" not in systemctl_log


def test_missing_profile_is_rejected_without_creating_it(tmp_path: Path) -> None:
    env, profile, _ = _fixture(tmp_path)
    shutil.rmtree(profile)
    result = _run(env, "--profile", "demo", "--skip-ml", "--no-timer", check=False)
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert not profile.exists()


def test_noninteractive_ml_requires_confirmation_before_changes(tmp_path: Path) -> None:
    env, profile, config = _fixture(tmp_path)
    original = config.read_bytes()

    result = _run(env, "--profile", "demo", "--no-timer", check=False)

    assert result.returncode != 0
    assert "confirmation is required" in result.stderr
    assert config.read_bytes() == original
    assert not (profile / "plugins").exists()
    assert not (profile / ".autonomy-install-backups").exists()


def test_config_is_restored_when_installation_fails(tmp_path: Path) -> None:
    env, profile, config = _fixture(tmp_path)
    original = config.read_bytes()
    old_plugin = profile / "plugins" / "autonomy"
    old_plugin.mkdir(parents=True)
    (old_plugin / "local-sentinel").write_text("old plugin\n", encoding="utf-8")
    env["FAKE_FAIL_KEY"] = "autonomy.data_dir"

    result = _run(env, "--profile", "demo", "--skip-ml", "--no-timer", "--yes", check=False)

    assert result.returncode != 0
    assert "restoring the original config.yaml" in result.stderr
    assert config.read_bytes() == original
    assert (old_plugin / "local-sentinel").read_text(encoding="utf-8") == "old plugin\n"
    assert not (profile / "plugins" / "seed_memory").exists()
    assert (profile / "seed_data" / "seeds" / "seeds.db").read_bytes() == b"seed-db-sentinel"
    assert (profile / "autonomy_data" / "state.db").read_bytes() == b"state-db-sentinel"


def test_existing_units_are_preserved_unless_force_is_given(tmp_path: Path) -> None:
    env, profile, _ = _fixture(tmp_path)
    unit_dir = profile.parents[2] / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service = unit_dir / "demo-autonomy.service"
    timer = unit_dir / "demo-autonomy.timer"
    service.write_text("old service\n", encoding="utf-8")
    timer.write_text("old timer\n", encoding="utf-8")

    _run(env, "--profile", "demo", "--skip-ml", "--yes")
    assert service.read_text(encoding="utf-8") == "old service\n"
    assert timer.read_text(encoding="utf-8") == "old timer\n"

    _run(env, "--profile", "demo", "--skip-ml", "--yes", "--force")
    assert "OnCalendar=*-*-* *:00/20:00" in timer.read_text(encoding="utf-8")
    assert "TimeoutStartSec=3000" in service.read_text(encoding="utf-8")
    backed_services = list((profile / ".autonomy-install-backups").glob("*/systemd/demo-autonomy.service"))
    backed_timers = list((profile / ".autonomy-install-backups").glob("*/systemd/demo-autonomy.timer"))
    assert backed_services and backed_services[-1].read_text(encoding="utf-8") == "old service\n"
    assert backed_timers and backed_timers[-1].read_text(encoding="utf-8") == "old timer\n"


def test_yes_is_forwarded_to_separate_ml_installer(tmp_path: Path) -> None:
    env, _, _ = _fixture(tmp_path)
    ml_log = tmp_path / "ml.log"
    ml = _executable(
        tmp_path / "fake-install-ml",
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$FAKE_ML_LOG"\n',
    )
    env["ML_INSTALLER"] = _bash_path(ml)
    env["FAKE_ML_LOG"] = _bash_path(ml_log)
    _run(env, "--profile", "demo", "--no-timer", "--yes")
    assert ml_log.read_text(encoding="utf-8").strip() == "--profile demo --yes"


def test_ml_installer_rejects_missing_profile_before_download(tmp_path: Path) -> None:
    env, profile, _ = _fixture(tmp_path)
    shutil.rmtree(profile)
    assert BASH is not None
    result = subprocess.run(
        [BASH, _bash_path(INSTALL_ML), "--profile", "demo", "--yes"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "refusing to create it" in result.stderr
    assert not profile.exists()
