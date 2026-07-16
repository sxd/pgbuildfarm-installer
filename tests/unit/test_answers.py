from __future__ import annotations

import json
import stat
import tempfile
from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module
from conftest import write_json


def test_default_answer_path_uses_temp_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    temp = tmp_path / "temp-cache"
    monkeypatch.setenv("TEMP", str(temp))

    inst = installer_factory()

    assert inst.cache_answers == temp / "pg-buildfarm-answers.json"


def test_default_answer_path_falls_back_to_python_tempdir(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    monkeypatch.delenv("TEMP", raising=False)

    inst = installer_factory()

    assert (
        inst.cache_answers == Path(tempfile.gettempdir()) / "pg-buildfarm-answers.json"
    )


def test_answers_file_overrides_default_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("TEMP", str(cache_dir))
    write_json(cache_dir / "pg-buildfarm-answers.json", {"animal": "cached"})

    explicit = tmp_path / "missing-explicit.json"
    inst = installer_factory(answers_file=explicit)

    assert inst.answers == {}

    write_json(explicit, {"animal": "explicit"})
    inst = installer_factory(answers_file=explicit)

    assert inst.answers["animal"] == "explicit"


def test_flat_answer_file_format_loads(tmp_path: Path, installer_factory) -> None:
    answers = tmp_path / "answers.json"
    write_json(answers, {"animal": "flat-animal"}, wrapped=False)

    inst = installer_factory(answers_file=answers)

    assert inst.answers == {"animal": "flat-animal"}


def test_loaded_answer_file_drops_sensitive_keys(
    tmp_path: Path, installer_factory
) -> None:
    answers = tmp_path / "answers.json"
    write_json(answers, {"animal": "saved-animal", "secret": "old-secret"})

    inst = installer_factory(answers_file=answers)

    assert inst.answers == {"animal": "saved-animal"}


def test_reset_answers_ignores_saved_file(tmp_path: Path, installer_factory) -> None:
    answers = tmp_path / "answers.json"
    write_json(answers, {"animal": "saved-animal"})

    inst = installer_factory(answers_file=answers, reset_answers=True)

    assert inst.answers == {}


def test_prompt_saved_skips_input_for_existing_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    answers = tmp_path / "answers.json"
    write_json(answers, {"animal": "saved-animal"})
    inst = installer_factory(answers_file=answers)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: pytest.fail(f"unexpected prompt: {prompt}")
    )

    assert inst.prompt_saved("animal", "Buildfarm animal name") == "saved-animal"


def test_secret_default_is_not_echoed_in_prompt(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(
        installer_module.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or "",
    )
    inst = installer_factory()

    assert (
        inst.prompt("Buildfarm secret", default="do-not-display", secret=True)
        == "do-not-display"
    )
    assert prompts == ["Buildfarm secret [saved]: "]


def test_saved_answer_file_is_written_0600(tmp_path: Path, installer_factory) -> None:
    answers = tmp_path / "answers.json"
    inst = installer_factory(answers_file=answers)

    inst.remember("animal", "mode-test")

    assert stat.S_IMODE(answers.stat().st_mode) == 0o600
    payload = json.loads(answers.read_text(encoding="utf-8"))
    assert payload["answers"]["animal"] == "mode-test"


def test_secret_answer_is_not_persisted(tmp_path: Path, installer_factory) -> None:
    answers = tmp_path / "answers.json"
    inst = installer_factory(answers_file=answers)

    inst.remember("secret", "do-not-write")
    assert not answers.exists()

    inst.remember("animal", "persisted-animal")

    payload = json.loads(answers.read_text(encoding="utf-8"))
    assert payload["answers"] == {"animal": "persisted-animal"}


def test_unsaved_prompt_answer_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installer_factory
) -> None:
    answers = tmp_path / "answers.json"
    inst = installer_factory(answers_file=answers)
    monkeypatch.setattr(inst, "prompt", lambda *args, **kwargs: "transient-secret")

    assert (
        inst.prompt_saved("secret", "Buildfarm secret", persist=False)
        == "transient-secret"
    )
    assert not answers.exists()
