from __future__ import annotations

import tarfile

import pytest

import pg_buildfarm_installer as installer_module


def _member(name: str, type_: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = type_
    return member


@pytest.mark.parametrize(
    "member",
    [
        _member("/absolute/path"),
        _member("../escape"),
        _member("client/../../escape"),
        _member("client/link", tarfile.SYMTYPE),
        _member("client/hardlink", tarfile.LNKTYPE),
        _member("client/fifo", tarfile.FIFOTYPE),
    ],
)
def test_unsafe_tar_members_are_rejected(member: tarfile.TarInfo) -> None:
    with pytest.raises(RuntimeError, match="unsafe tar member"):
        installer_module.Installer.validate_tar_member(member)


@pytest.mark.parametrize(
    "member",
    [
        _member("client"),
        _member("client/run_branches.pl"),
        _member("client/lib/PGBuild/Options.pm"),
    ],
)
def test_normal_relative_tar_members_are_allowed(member: tarfile.TarInfo) -> None:
    installer_module.Installer.validate_tar_member(member)


def test_tar_member_count_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer_module, "MAX_CLIENT_ARCHIVE_MEMBERS", 1)

    with pytest.raises(RuntimeError, match="too many members"):
        installer_module.Installer.validate_tar_members(
            [_member("client/a"), _member("client/b")]
        )


def test_tar_file_and_total_size_limits_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer_module, "MAX_CLIENT_ARCHIVE_FILE_BYTES", 3)
    member = _member("client/large")
    member.size = 4

    with pytest.raises(RuntimeError, match="member is too large"):
        installer_module.Installer.validate_tar_members([member])

    monkeypatch.setattr(installer_module, "MAX_CLIENT_ARCHIVE_FILE_BYTES", 10)
    monkeypatch.setattr(installer_module, "MAX_CLIENT_ARCHIVE_TOTAL_BYTES", 5)
    first = _member("client/one")
    first.size = 3
    second = _member("client/two")
    second.size = 3

    with pytest.raises(RuntimeError, match="expands to too much data"):
        installer_module.Installer.validate_tar_members([first, second])


def test_compressed_download_size_limit_is_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class Response:
        headers = {"Content-Length": "4"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(installer_module, "MAX_CLIENT_ARCHIVE_BYTES", 3)
    monkeypatch.setattr(
        installer_module.urllib.request, "urlopen", lambda _: Response()
    )
    inst = object.__new__(installer_module.Installer)

    with pytest.raises(RuntimeError, match="download is too large"):
        inst.download_client_archive(tmp_path / "client.tgz")
