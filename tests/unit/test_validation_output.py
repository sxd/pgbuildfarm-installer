from __future__ import annotations

from pathlib import Path

from conftest import make_choices


def test_final_validation_instructions_are_no_send_no_status(
    capsys, tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    inst.unit_dir = tmp_path / ".config" / "systemd" / "user"

    inst.print_next_steps(make_choices(tmp_path / "root"), tmp_path / "root" / "client", tmp_path / "root" / "build-farm.conf")

    output = capsys.readouterr().out
    assert "--nosend --nostatus" in output
    assert "A successful validation ends with a zero exit status" in output
    assert "echo $?" in output
    assert "Register the animal" in output

