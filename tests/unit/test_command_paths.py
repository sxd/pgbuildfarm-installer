from __future__ import annotations

from pathlib import Path

import pg_buildfarm_installer as installer_module


def test_resolve_command_path_preserves_symlink(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target-command"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    command = tmp_path / "stable-command"
    command.symlink_to(target)
    monkeypatch.setattr(installer_module.shutil, "which", lambda _: str(command))

    resolved = installer_module.resolve_command_path(["command"])

    assert resolved == str(command.absolute())
    assert Path(resolved).is_symlink()
