from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pg_buildfarm_installer as installer_module


def write_json(path: Path, answers: dict[str, object], *, wrapped: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object]
    if wrapped:
        payload = {"version": installer_module.STATE_VERSION, "answers": answers}
    else:
        payload = answers
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def temp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_uid = os.geteuid()
    monkeypatch.setattr(installer_module.os, "geteuid", lambda: real_uid)
    monkeypatch.setattr(
        installer_module.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="buildfarm-test", pw_dir=str(home)),
    )
    return home


@pytest.fixture
def installer_factory(temp_home: Path):
    def make_installer(
        *,
        answers_file: Path | None = None,
        reset_answers: bool = False,
        dry_run: bool = False,
        check_only: bool = False,
    ) -> installer_module.Installer:
        args = SimpleNamespace(
            answers_file=answers_file,
            reset_answers=reset_answers,
            dry_run=dry_run,
            check_only=check_only,
        )
        return installer_module.Installer(args)

    return make_installer


def make_choices(
    root: Path,
    *,
    build_system: str = "make",
    client_local_path: Path | None = None,
    branches: list[str] | None = None,
    calendar: str = "*-*-* 00,12:00:00",
) -> installer_module.Choices:
    return installer_module.Choices(
        root=root,
        client_source="3",
        client_local_path=client_local_path,
        pg_remote=installer_module.PG_REMOTES["1"],
        mirror_mode="1",
        branches=branches or ["HEAD", "REL_19_STABLE"],
        calendar=calendar,
        animal="test-animal",
        secret="",
        build_system=build_system,
        make="make",
        config_opts=installer_module.MAKE_PRESETS["1"][1].copy(),
        meson_opts=installer_module.MESON_PRESETS["1"][1].copy(),
        meson_jobs="2",
        meson_test_timeout="3",
        extra_path="",
    )


def complete_answers(
    root: Path, fake_client: Path, *, build_system: str
) -> dict[str, object]:
    answers: dict[str, object] = {
        "path_choice": "3",
        "custom_root": str(root),
        "client_source": "3",
        "client_local_path": str(fake_client),
        "remote_choice": "1",
        "mirror_mode": "1",
        "branch_choice": "3",
        "branches": ["HEAD", "REL_19_STABLE"],
        "schedule": "2",
        "animal": "test-animal",
        "secret": "",
        "extra_path": "",
    }
    if build_system == "make":
        answers.update(
            {
                "build_system_choice": "1",
                "make": "make",
                "config_preset": "1",
            }
        )
    else:
        answers.update(
            {
                "build_system_choice": "2",
                "meson_preset": "1",
                "meson_jobs": "2",
                "meson_test_timeout": "3",
            }
        )
    return answers


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def fake_toolchain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = sys.executable

    _write_executable(
        bin_dir / "perl",
        f"""#!{python}
import os
import sys

args = sys.argv[1:]
if args and args[0].startswith("-M"):
    raise SystemExit(0)
if args[:1] == ["-cw"]:
    raise SystemExit(0)
if args and args[0].endswith(".pl"):
    os.execv(args[0], args)
raise SystemExit(0)
""",
    )
    _write_executable(
        bin_dir / "git",
        f"""#!{python}
import sys

args = sys.argv[1:]
if args[:2] == ["ls-remote", "--heads"]:
    print("0000000000000000000000000000000000000000\\trefs/heads/REL_19_STABLE")
    print("0000000000000000000000000000000000000000\\trefs/heads/REL_18_STABLE")
    print("0000000000000000000000000000000000000000\\trefs/heads/REL_17_STABLE")
    print("0000000000000000000000000000000000000000\\trefs/heads/REL_16_STABLE")
raise SystemExit(0)
""",
    )
    for name in (
        "make",
        "cc",
        "bison",
        "flex",
        "tar",
        "gzip",
        "systemctl",
        "systemd-analyze",
        "meson",
        "ninja",
    ):
        _write_executable(bin_dir / name, f"#!{python}\nraise SystemExit(0)\n")

    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    for label, executable in {
        "perl": "perl",
        "git": "git",
        "make": "make",
        "compiler": "cc",
        "bison": "bison",
        "flex": "flex",
        "tar": "tar",
        "gzip": "gzip",
        "systemctl": "systemctl",
        "systemd-analyze": "systemd-analyze",
        "meson": "meson",
        "ninja": "ninja",
    }.items():
        monkeypatch.setitem(
            installer_module.COMMAND_PATHS, label, str((bin_dir / executable).resolve())
        )
    return bin_dir


@pytest.fixture
def fake_client_source(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "fake-client"
    dest = tmp_path / "fake-client"
    shutil.copytree(source, dest)
    for directory in [dest, *[path for path in dest.rglob("*") if path.is_dir()]]:
        directory.chmod(0o755)
    for script in dest.glob("*.pl"):
        script.chmod(0o755)
    return dest
