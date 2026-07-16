from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import make_choices


def _assert_perl_syntax_if_available(tmp_path: Path, text: str) -> None:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl is not installed")
    config = tmp_path / "build-farm.conf"
    config.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [perl, "-cw", str(config)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr


def test_make_config_contains_required_buildfarm_keys(
    tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="make")

    text = inst.config(
        choices, tmp_path / "root" / "client", tmp_path / "root" / "postgresql.git"
    )

    assert "animal => 'test-animal'" in text
    assert "target => 'https://buildfarm.postgresql.org/cgi-bin/pgstatus.pl'" in text
    assert (
        "upgrade_target => 'https://buildfarm.postgresql.org/cgi-bin/upgrade.pl'"
        in text
    )
    assert "config_env => \\%config_env" in text
    assert "build_env => \\%build_env" in text
    assert "using_meson => 0" in text
    assert "my @config_opts = (" in text
    assert "'--enable-cassert'" in text
    assert "'--enable-debug'" in text
    assert "'--enable-tap-tests'" in text
    assert "my @meson_opts" not in text
    assert "# push @config_opts, '--enable-injection-points'" in text
    assert "config_opts => \\@config_opts" in text
    assert "branches_to_build => [ 'HEAD', 'REL_19_STABLE' ]" in text
    _assert_perl_syntax_if_available(tmp_path, text)


def test_meson_config_contains_required_buildfarm_keys(
    tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="meson")

    text = inst.config(
        choices, tmp_path / "root" / "client", tmp_path / "root" / "postgresql.git"
    )

    assert "using_meson => 1" in text
    assert "my @meson_opts = (" in text
    assert "meson_opts => \\@meson_opts" in text
    assert "'-Ddebug=true'" in text
    assert "my @config_opts" not in text
    assert "# push @meson_opts, '-Dinjection_points=true'" in text
    assert "meson_jobs => 2" in text
    assert "meson_test_timeout => 3" in text
    assert "target => 'https://buildfarm.postgresql.org/cgi-bin/pgstatus.pl'" in text
    assert (
        "upgrade_target => 'https://buildfarm.postgresql.org/cgi-bin/upgrade.pl'"
        in text
    )
    assert "config_env => \\%config_env" in text
    assert "build_env => \\%build_env" in text
    assert "branches_to_build => [ 'HEAD', 'REL_19_STABLE' ]" in text
    _assert_perl_syntax_if_available(tmp_path, text)


@pytest.mark.parametrize("jobs", ["0", "١", "２"])
def test_meson_config_rejects_non_positive_or_non_ascii_jobs(
    tmp_path: Path, installer_factory, jobs: str
) -> None:
    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="meson")
    choices.meson_jobs = jobs

    with pytest.raises(RuntimeError, match="Meson jobs must be a positive integer"):
        inst.config(choices, tmp_path / "root" / "client", tmp_path / "root" / "mirror")


def test_meson_config_rejects_non_ascii_timeout_and_normalizes_timeout(
    tmp_path: Path, installer_factory
) -> None:
    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="meson")
    choices.meson_test_timeout = "٠"

    with pytest.raises(RuntimeError, match="Meson test timeout"):
        inst.config(choices, tmp_path / "root" / "client", tmp_path / "root" / "mirror")

    choices.meson_test_timeout = "0003"
    text = inst.config(
        choices, tmp_path / "root" / "client", tmp_path / "root" / "mirror"
    )
    assert "meson_test_timeout => 3" in text


def test_extra_path_is_rendered_in_build_env(tmp_path: Path, installer_factory) -> None:
    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="make")
    choices.extra_path = "/custom/bin"

    text = inst.config(
        choices, tmp_path / "root" / "client", tmp_path / "root" / "postgresql.git"
    )

    assert "$build_env{PATH} = '/custom/bin' . ':' . $ENV{PATH};" in text
    assert "build_env => \\%build_env" in text
    _assert_perl_syntax_if_available(tmp_path, text)


def test_feature_preset_keeps_original_options_and_comments_extra_options(
    tmp_path: Path, installer_factory
) -> None:
    import pg_buildfarm_installer as installer_module

    inst = installer_factory()
    choices = make_choices(tmp_path / "root", build_system="meson")
    choices.meson_opts = installer_module.MESON_PRESETS["2"][1].copy()

    text = inst.config(
        choices, tmp_path / "root" / "client", tmp_path / "root" / "postgresql.git"
    )

    assert "'-Dcassert=true'," in text
    assert "'-Ddebug=true'," in text
    assert "'-Dssl=openssl'," in text
    assert "'-Dzlib=enabled'," in text
    assert "# '-Dllvm=enabled'," in text
    assert "# '-Dselinux=disabled'," in text
    assert "# push @meson_opts, '-Dinjection_points=true'" in text
    assert "#   -Dlibcurl=enabled" in text
    assert "#   -DRANDOMIZE_ALLOCATED_MEMORY" in text
    assert "# push @cppflags, '-DREALLOCATE_BITMAPSETS'" in text
    assert "# $build_env{PG_TEST_EXTRA}" in text
    assert "#   checksum_extended" in text
    assert "my @config_opts" not in text
    _assert_perl_syntax_if_available(tmp_path, text)
