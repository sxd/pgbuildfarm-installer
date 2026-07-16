from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module
from conftest import complete_answers, write_json


@pytest.mark.parametrize("build_system", ["make", "meson"])
def test_offline_fake_client_e2e_for_build_system(
    build_system: str,
    fake_toolchain: Path,
    fake_client_source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_factory,
) -> None:
    root = tmp_path / f"install-{build_system}"
    answers_file = tmp_path / f"{build_system}-answers.json"
    write_json(
        answers_file,
        complete_answers(root, fake_client_source, build_system=build_system),
    )
    monkeypatch.setattr(installer_module.getpass, "getpass", lambda prompt: "")
    inst = installer_factory(answers_file=answers_file)

    assert inst.execute() == 0

    client_dir = root / "client"
    config = root / "build-farm.conf"
    assert client_dir.exists()
    assert config.exists()
    assert (inst.unit_dir / "pg-buildfarm.service").exists()
    assert (inst.unit_dir / "pg-buildfarm.timer").exists()

    env = os.environ.copy()
    env["FAKE_EXPECTED_BUILD_SYSTEM"] = build_system
    env["FAKE_EXPECTED_BRANCHES"] = "HEAD,REL_19_STABLE"
    result = subprocess.run(
        [
            "./run_branches.pl",
            "--run-all",
            "--nosend",
            "--nostatus",
            "--verbose",
            "--config",
            str(config),
        ],
        cwd=client_dir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "FAKE_BUILDFARM_VALIDATION_OK" in result.stdout


def test_e2e_resumes_non_secret_answers_from_default_temp_answers(
    monkeypatch: pytest.MonkeyPatch,
    fake_toolchain: Path,
    fake_client_source: Path,
    tmp_path: Path,
    installer_factory,
) -> None:
    temp_dir = tmp_path / "temp"
    monkeypatch.setenv("TEMP", str(temp_dir))
    root = tmp_path / "resume-install"
    write_json(
        temp_dir / "pg-buildfarm-answers.json",
        complete_answers(root, fake_client_source, build_system="make"),
    )
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"unexpected prompt: {prompt}")
    )
    monkeypatch.setattr(installer_module.getpass, "getpass", lambda prompt: "")
    inst = installer_factory()

    assert inst.execute() == 0
    assert (root / "client" / "run_branches.pl").exists()
    assert (root / "build-farm.conf").exists()
