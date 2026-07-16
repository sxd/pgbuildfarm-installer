from __future__ import annotations

from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module
from conftest import complete_answers, write_json


@pytest.mark.parametrize(
    ("manager", "missing", "expected"),
    [
        (
            "apt",
            ["perl", "ninja", "Mozilla::CA"],
            ["libmozilla-ca-perl", "ninja-build", "perl"],
        ),
        ("dnf", ["perl", "LWP::Protocol::https"], ["perl", "perl-LWP-Protocol-https"]),
        ("apk", ["meson", "ninja"], ["meson", "samurai"]),
    ],
)
def test_packages_for_known_managers(
    manager: str, missing: list[str], expected: list[str]
) -> None:
    assert installer_module.Installer.packages_for(manager, missing) == expected


def test_install_command_is_report_only_text() -> None:
    assert (
        installer_module.Installer.install_command("apt", ["git", "perl"])
        == "sudo apt install git perl"
    )
    assert (
        installer_module.Installer.install_command("apk", ["samurai"])
        == "sudo apk add samurai"
    )


def test_build_system_dependency_check_reports_missing_tools(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    inst = installer_factory()
    monkeypatch.setattr(
        installer_module.Installer,
        "command_available",
        staticmethod(lambda commands: False),
    )

    assert inst.build_system_dependency_check("meson") == ["meson", "ninja"]
    assert inst.build_system_dependency_check("make") == ["make"]


def test_check_only_reports_make_and_meson_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installer_factory,
) -> None:
    inst = installer_factory(check_only=True)
    monkeypatch.setattr(inst, "dependency_check", lambda: [])
    monkeypatch.setattr(
        inst,
        "build_system_dependency_check",
        lambda build_system: {
            "make": ["make"],
            "meson": ["meson", "ninja"],
        }[build_system],
    )

    assert inst.execute() == 1

    output = capsys.readouterr().out
    assert "Missing make/configure build-system prerequisites: make" in output
    assert "Missing Meson build-system prerequisites: meson, ninja" in output


def test_check_only_reports_available_build_system_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    installer_factory,
) -> None:
    inst = installer_factory(check_only=True)
    monkeypatch.setattr(inst, "dependency_check", lambda: [])
    monkeypatch.setattr(inst, "build_system_dependency_check", lambda build_system: [])

    assert inst.execute() == 0

    output = capsys.readouterr().out
    assert "make/configure build-system prerequisites are available." in output
    assert "Meson build-system prerequisites are available." in output


def test_command_available_uses_global_command_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "pgbuildfarm-tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setitem(installer_module.COMMAND_PATHS, "make", str(tool.resolve()))
    monkeypatch.setattr(
        installer_module.shutil,
        "which",
        lambda command: pytest.fail("command_available should use COMMAND_PATHS"),
    )

    assert installer_module.Installer.command_path("make") == str(tool.resolve())
    assert installer_module.Installer.command_available("make")


def test_make_collect_defaults_to_resolved_make_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client_source: Path,
    installer_factory,
) -> None:
    root = tmp_path / "install"
    answers = complete_answers(root, fake_client_source, build_system="make")
    answers.pop("make")
    answers_file = tmp_path / "answers.json"
    write_json(answers_file, answers)
    gmake = tmp_path / "bin" / "gmake"
    gmake.parent.mkdir()
    gmake.write_text("#!/bin/sh\n", encoding="utf-8")
    gmake.chmod(0o755)
    monkeypatch.setitem(installer_module.COMMAND_PATHS, "make", str(gmake))
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(installer_module.getpass, "getpass", lambda prompt: "")
    inst = installer_factory(answers_file=answers_file, dry_run=True)

    choices = inst.collect()

    assert choices.make == str(gmake)
