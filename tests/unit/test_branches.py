from __future__ import annotations

from pathlib import Path

import pytest

import pg_buildfarm_installer as installer_module


def test_default_branch_list_uses_current_rel_stable_shape() -> None:
    assert installer_module.DEFAULT_BRANCHES == [
        "HEAD",
        "REL_19_STABLE",
        "REL_18_STABLE",
        "REL_17_STABLE",
        "REL_16_STABLE",
    ]
    assert all(installer_module.BRANCH_RE.fullmatch(branch) for branch in installer_module.DEFAULT_BRANCHES)
    assert not any(branch.startswith("REL19") for branch in installer_module.DEFAULT_BRANCHES)


def test_latest_branches_are_sorted_newest_first(installer_factory) -> None:
    inst = installer_factory()

    assert inst.latest_branches_from(
        ["REL_17_STABLE", "REL_19_STABLE", "HEAD", "REL_16_STABLE", "REL_18_STABLE"],
        3,
    ) == ["HEAD", "REL_19_STABLE", "REL_18_STABLE", "REL_17_STABLE"]


def test_default_branches_keep_fallback_even_if_discovery_is_missing(installer_factory) -> None:
    inst = installer_factory()

    assert inst.default_branches_from(["HEAD", "REL_18_STABLE"]) == installer_module.DEFAULT_BRANCHES


def test_custom_branch_parser_accepts_valid_names(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    inst = installer_factory()
    monkeypatch.setattr(inst, "prompt", lambda text: "HEAD, REL_19_STABLE")

    assert inst.parse_custom_branches() == ["HEAD", "REL_19_STABLE"]


def test_custom_branch_parser_rejects_invalid_rel_shape(
    monkeypatch: pytest.MonkeyPatch, installer_factory
) -> None:
    inst = installer_factory()
    monkeypatch.setattr(inst, "prompt", lambda text: "HEAD, REL19_STABLE")

    with pytest.raises(RuntimeError, match="REL19_STABLE"):
        inst.parse_custom_branches()

