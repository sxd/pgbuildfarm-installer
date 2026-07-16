from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module
from conftest import make_choices


@pytest.mark.parametrize(
    "calendar",
    [
        "*-*-* 03:00:00",
        "*-*-* 00,12:00:00",
        "*-*-* 00,06,12,18:00:00",
        "Mon,Fri *-*-* 02:30:00",
    ],
)
def test_user_systemd_units_encode_schedule_and_no_system_scope(
    tmp_path: Path, installer_factory, calendar: str
) -> None:
    inst = installer_factory()
    inst.unit_dir = tmp_path / ".config" / "systemd" / "user"
    choices = make_choices(tmp_path / "root", calendar=calendar)

    inst.write_units(
        choices,
        tmp_path / "root" / "client",
        tmp_path / "root" / "build-farm.conf",
        tmp_path / "root" / "postgresql.git",
    )

    service = (inst.unit_dir / "pg-buildfarm.service").read_text(encoding="utf-8")
    timer = (inst.unit_dir / "pg-buildfarm.timer").read_text(encoding="utf-8")
    assert f"WorkingDirectory={tmp_path}/root/client" in service
    assert 'WorkingDirectory="' not in service
    assert 'ExecStart="/' in service
    assert "--nosend" in service
    assert "--nostatus" in service
    assert f"OnCalendar={calendar}" in timer
    assert "Persistent=true" in timer
    assert "/etc/systemd/system" not in service + timer


def test_next_steps_show_validation_and_user_systemctl(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    inst.unit_dir = tmp_path / ".config" / "systemd" / "user"

    inst.print_next_steps(
        make_choices(tmp_path / "root"),
        tmp_path / "root" / "client",
        tmp_path / "root" / "build-farm.conf",
    )

    output = capsys.readouterr().out
    assert "cd " in output
    assert (
        "./run_branches.pl --run-all --nosend --nostatus --verbose --config" in output
    )
    assert "echo $?" in output
    assert "systemctl --user daemon-reload" in output
    assert "systemctl --user status pg-buildfarm.timer" in output
    assert "Register the animal" in output
    assert "/etc/systemd/system" not in output


def test_next_steps_include_registration_system_information(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    installer_factory,
) -> None:
    inst = installer_factory()
    monkeypatch.setattr(
        inst,
        "registration_system_info",
        lambda: [
            ("Operating System", "TestOS"),
            ("OS Version", "1.2"),
            ("Compiler", "testcc"),
            ("Compiler Version", "testcc 3.4"),
            ("Architecture", "testarch"),
        ],
    )

    inst.print_next_steps(
        make_choices(tmp_path / "root"),
        tmp_path / "root" / "client",
        tmp_path / "root" / "build-farm.conf",
    )

    output = capsys.readouterr().out
    assert "Use this system information when registering the animal" in output
    assert "Operating System: TestOS" in output
    assert "OS Version: 1.2" in output
    assert "Compiler: testcc" in output
    assert "Compiler Version: testcc 3.4" in output
    assert "Architecture: testarch" in output


def test_compiler_registration_info_clarifies_gcc_behind_cc(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    inst = installer_factory()
    monkeypatch.setitem(installer_module.COMMAND_PATHS, "compiler", "/usr/bin/cc")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == ["/usr/bin/cc", "-dM", "-E", "-"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="#define __GNUC__ 15\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="cc (Ubuntu 15.2.0-16ubuntu1) 15.2.0\n",
            stderr="",
        )

    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    assert inst.compiler_registration_info() == (
        "gcc (via cc)",
        "cc (Ubuntu 15.2.0-16ubuntu1) 15.2.0",
    )


def test_compiler_registration_info_clarifies_clang_behind_cc(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    inst = installer_factory()
    monkeypatch.setitem(installer_module.COMMAND_PATHS, "compiler", "/usr/bin/cc")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command == ["/usr/bin/cc", "-dM", "-E", "-"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="#define __clang__ 1\n#define __GNUC__ 4\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="cc version 18.1.3\n",
            stderr="",
        )

    monkeypatch.setattr(installer_module.subprocess, "run", fake_run)

    assert inst.compiler_registration_info() == (
        "clang (via cc)",
        "cc version 18.1.3",
    )


def test_calendar_values_reject_line_injection(installer_factory) -> None:
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="cannot contain newlines"):
        inst.validate_calendar("*-*-* 03:00:00\nUnit=evil.service")


def test_systemd_argument_escapes_dollar_signs(installer_factory) -> None:
    inst = installer_factory()

    assert inst.systemd_arg("$HOME/$USER") == '"$$HOME/$$USER"'


def test_systemd_working_directory_uses_unquoted_path_escape(
    tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    inst.unit_dir = tmp_path / ".config" / "systemd" / "user"
    client_dir = tmp_path / "root dir" / "client"

    inst.write_units(
        make_choices(tmp_path / "root"),
        client_dir,
        tmp_path / "root" / "build-farm.conf",
        tmp_path / "root" / "postgresql.git",
    )

    service = (inst.unit_dir / "pg-buildfarm.service").read_text(encoding="utf-8")
    assert f"WorkingDirectory={tmp_path}/root\\x20dir/client" in service
    assert f'WorkingDirectory="{client_dir}"' not in service


def test_next_steps_shell_quote_paths(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    client = tmp_path / "root dir;touch nope" / "client"
    config = tmp_path / "root dir;touch nope" / "build farm.conf"

    inst.print_next_steps(make_choices(tmp_path / "root"), client, config)

    output = capsys.readouterr().out
    assert f"cd '{client}'" in output
    assert f"--config '{config}'" in output
