from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module
from conftest import make_choices


def _make_client_tree(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o755)
    for name in ("run_branches.pl", "run_build.pl", "run_web_txn.pl"):
        script = path / name
        script.write_text("#!/usr/bin/env perl\n1;\n", encoding="utf-8")
        script.chmod(0o755)
    return path


def test_verify_client_dir_rejects_symlink_script(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    (client / "run_web_txn.pl").unlink()
    (client / "run_web_txn.pl").symlink_to(client / "run_build.pl")
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="unsafe script file"):
        inst.verify_client_dir(client)


def test_verify_local_client_source_rejects_group_writable_path(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    client.chmod(client.stat().st_mode | stat.S_IWGRP)
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="group/world-writable"):
        inst.verify_local_client_source(client)


def test_harden_client_permissions_removes_group_world_write(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    nested = client / "nested"
    nested.mkdir()
    writable_file = nested / "file"
    writable_file.write_text("content\n", encoding="utf-8")
    for path in (client, nested, writable_file):
        path.chmod(path.stat().st_mode | stat.S_IWGRP | stat.S_IWOTH)
    inst = installer_factory()

    inst.harden_client_permissions(client)

    inst.verify_client_dir(client)
    for path in (client, nested, writable_file):
        assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_install_client_hardens_group_writable_staged_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    root = tmp_path / "root"
    inst = installer_factory()
    choices = make_choices(root)
    choices.client_source = "2"
    choices.client_local_path = None
    monkeypatch.setitem(installer_module.COMMAND_PATHS, "git", "/usr/bin/git")

    def fake_clone(
        command: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        staged_client = Path(command[-1])
        _make_client_tree(staged_client)
        for path in (staged_client, *(p for p in staged_client.rglob("*"))):
            path.chmod(path.stat().st_mode | stat.S_IWGRP | stat.S_IWOTH)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(inst, "run", fake_clone)

    inst.install_client(choices, root / "client")

    published_client = root / "client"
    inst.verify_client_dir(published_client)
    for path in (published_client, *(p for p in published_client.rglob("*"))):
        assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_verify_local_client_source_rejects_symlink_inside_tree(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    (client / "linked").symlink_to(client / "run_build.pl")
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="contains symlink"):
        inst.verify_local_client_source(client)


def test_verify_client_dir_rejects_symlink_root(
    tmp_path: Path, installer_factory
) -> None:
    target = _make_client_tree(tmp_path / "target")
    client = tmp_path / "client"
    client.symlink_to(target, target_is_directory=True)
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="contains symlink"):
        inst.verify_client_dir(client)


def test_verify_local_client_source_rejects_special_file(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    fifo = client / "fifo"
    os.mkfifo(fifo)
    inst = installer_factory()

    with pytest.raises(RuntimeError, match="unsupported file type"):
        inst.verify_local_client_source(client)


def test_verify_local_client_source_accepts_regular_tree(
    tmp_path: Path, installer_factory
) -> None:
    client = _make_client_tree(tmp_path / "client")
    inst = installer_factory()

    inst.verify_local_client_source(client)


def test_local_copy_failure_does_not_leave_client_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    source = _make_client_tree(tmp_path / "source")
    root = tmp_path / "root"
    inst = installer_factory()

    def fail_copytree(source: Path, destination: Path, **kwargs: object) -> None:
        raise shutil.Error("copy failed")

    monkeypatch.setattr(shutil, "copytree", fail_copytree)

    with pytest.raises(RuntimeError, match="unable to copy local buildfarm client"):
        inst.install_client(
            make_choices(root, client_local_path=source), root / "client"
        )

    assert not (root / "client").exists()
