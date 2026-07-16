#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Interactive, user-only PostgreSQL Buildfarm client installer (Python 3.12+)."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CLIENT_TARBALL = "https://buildfarm.postgresql.org/downloads/latest-client.tgz"
CLIENT_GIT = "https://github.com/PGBuildFarm/client-code"
BUILDFARM_TARGET = "https://buildfarm.postgresql.org/cgi-bin/pgstatus.pl"
BUILDFARM_UPGRADE_TARGET = "https://buildfarm.postgresql.org/cgi-bin/upgrade.pl"
STATE_VERSION = 1
SENSITIVE_ANSWER_KEYS = {"secret"}
MAX_CLIENT_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_CLIENT_ARCHIVE_MEMBERS = 10_000
MAX_CLIENT_ARCHIVE_FILE_BYTES = 100 * 1024 * 1024
MAX_CLIENT_ARCHIVE_TOTAL_BYTES = 500 * 1024 * 1024
COMMAND_CANDIDATES = {
    "apt": ["apt"],
    "dnf": ["dnf"],
    "yum": ["yum"],
    "zypper": ["zypper"],
    "pacman": ["pacman"],
    "apk": ["apk"],
    "perl": ["perl"],
    "git": ["git"],
    "make": ["make", "gmake"],
    "compiler": ["cc", "gcc", "clang"],
    "bison": ["bison"],
    "flex": ["flex"],
    "tar": ["tar"],
    "gzip": ["gzip"],
    "systemctl": ["systemctl"],
    "systemd-analyze": ["systemd-analyze"],
    "meson": ["meson"],
    "ninja": ["ninja", "ninja-build", "samurai", "samu"],
}
PG_REMOTES = {
    "1": "https://git.postgresql.org/git/postgresql.git",
    "2": "https://github.com/postgres/postgres.git",
}
DEFAULT_BRANCHES = [
    "HEAD",
    "REL_19_STABLE",
    "REL_18_STABLE",
    "REL_17_STABLE",
    "REL_16_STABLE",
]
BRANCH_RE = re.compile(r"^(HEAD|REL_\d+_STABLE)$")
MAKE_PRESETS = {
    "1": (
        "common hacker/assertion build",
        ["--enable-cassert", "--enable-debug", "--enable-tap-tests"],
    ),
    "2": (
        "feature buildfarm-style build",
        [
            "--enable-cassert",
            "--enable-debug",
            "--enable-nls",
            "--enable-tap-tests",
            "--with-perl",
            "--with-python",
            "--with-tcl",
            "--with-openssl",
            "--with-icu",
            "--with-libxml",
            "--with-libxslt",
        ],
    ),
}
MESON_PRESETS = {
    "1": (
        "common hacker/assertion build",
        ["-Dcassert=true", "-Ddebug=true", "-Dtap_tests=enabled"],
    ),
    "2": (
        "feature buildfarm-style build",
        [
            "-Dcassert=true",
            "-Ddebug=true",
            "-Dtap_tests=enabled",
            "-Dnls=enabled",
            "-Dplperl=enabled",
            "-Dplpython=enabled",
            "-Dpltcl=enabled",
            "-Dssl=openssl",
            "-Dicu=enabled",
            "-Dlibxml=enabled",
            "-Dlibxslt=enabled",
            "-Dzlib=enabled",
            "-Dreadline=enabled",
        ],
    ),
}
MAKE_EXTRA_OPTIONS = [
    "--with-llvm",
    "--with-lz4",
    "--with-zstd",
    "--with-ssl=openssl",
    "--with-gssapi",
    "--with-ldap",
    "--with-pam",
    "--with-systemd",
    "--with-bonjour",
    "--with-uuid=e2fs",
    "--with-zlib",
    "--with-readline",
    "--without-selinux",
]
MESON_EXTRA_OPTIONS = [
    "-Dllvm=enabled",
    "-Dlz4=enabled",
    "-Dzstd=enabled",
    "-Dgssapi=enabled",
    "-Dldap=enabled",
    "-Dpam=enabled",
    "-Dsystemd=enabled",
    "-Dbonjour=enabled",
    "-Duuid=e2fs",
    "-Dselinux=disabled",
]
CPPFLAGS_EXTRA_OPTIONS = [
    "-DRANDOMIZE_ALLOCATED_MEMORY",
    "-DRELCACHE_FORCE_RELEASE",
    "-DCATCACHE_FORCE_RELEASE",
    "-DRECOVER_RELATION_BUILD_MEMORY=1",
    "-DCOPY_PARSE_PLAN_TREES",
    "-DWRITE_READ_PARSE_PLAN_TREES",
    "-DRAW_EXPRESSION_COVERAGE_TEST",
]
PG_TEST_EXTRA_OPTIONS = [
    "kerberos",
    "ldap",
    "ssl",
    "load_balance",
    "wal_consistency_checking",
]


def resolve_command_path(commands: list[str]) -> str | None:
    for command in commands:
        path = shutil.which(command)
        if path:
            # Keep the executable selected by PATH.  Resolving it would replace a
            # stable command symlink (for example an alternatives-managed tool)
            # with its current target.
            return os.path.abspath(path)
    return None


COMMAND_PATHS = {
    label: resolve_command_path(commands)
    for label, commands in COMMAND_CANDIDATES.items()
}


@dataclass
class Choices:
    root: Path
    client_source: str
    client_local_path: Path | None
    pg_remote: str
    mirror_mode: str
    branches: list[str]
    calendar: str
    animal: str
    secret: str
    build_system: str
    make: str
    config_opts: list[str]
    meson_opts: list[str]
    meson_jobs: str
    meson_test_timeout: str
    extra_path: str


class Installer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        account = pwd.getpwuid(os.geteuid())
        self.user = account.pw_name
        self.home = Path(account.pw_dir).resolve()
        self.unit_dir = self.home / ".config/systemd/user"
        self.cache_dir = Path(
            os.environ.get("TEMP", tempfile.gettempdir())
        ).expanduser()
        self.cache_answers = self.cache_dir / "pg-buildfarm-answers.json"
        self.answers: dict[str, object] = self.load_answers()
        self.actions: list[str] = []

    def say(self, message: str) -> None:
        print(message)

    def run(
        self, command: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str] | None:
        display = " ".join(command)
        if self.args.dry_run:
            self.say(f"DRY-RUN: {display}")
            return None
        self.actions.append(display)
        return subprocess.run(command, cwd=cwd, check=check, text=True)

    @staticmethod
    def command_path(label: str) -> str | None:
        return COMMAND_PATHS.get(label)

    @classmethod
    def require_command(cls, label: str) -> str:
        path = cls.command_path(label)
        if path is None:
            raise RuntimeError(f"{label} is required but was not found on PATH")
        return path

    def write(self, path: Path, content: str, mode: int = 0o644) -> None:
        if self.args.dry_run:
            self.say(f"DRY-RUN: write {path} ({oct(mode)})")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RuntimeError(f"refusing to overwrite symlink: {path}")
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(tmp_fd, mode)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(content)
            Path(tmp_name).replace(path)
            path.chmod(mode)
        except Exception:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            Path(tmp_name).unlink(missing_ok=True)
            raise
        self.actions.append(f"write {path}")

    def load_answers(self) -> dict[str, object]:
        if self.args.reset_answers:
            return {}
        paths = (
            (self.args.answers_file,)
            if self.args.answers_file is not None
            else (self.cache_answers,)
        )
        for path in paths:
            if path is None or not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.say(f"Ignoring unreadable answers file {path}: {exc}")
                continue
            if not isinstance(data, dict):
                self.say(f"Ignoring answers file {path}: expected a JSON object.")
                continue
            if "answers" not in data and "version" not in data:
                self.say(f"Loaded answers from {path}.")
                return self.sanitize_answers(data)
            if data.get("version") != STATE_VERSION or not isinstance(
                data.get("answers"), dict
            ):
                self.say(f"Ignoring incompatible answers file {path}.")
                continue
            self.say(f"Loaded saved answers from {path}.")
            return self.sanitize_answers(data["answers"])
        return {}

    @staticmethod
    def sanitize_answers(answers: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in answers.items()
            if key not in SENSITIVE_ANSWER_KEYS
        }

    def save_answers(self) -> None:
        if self.args.dry_run:
            return
        target = self.args.answers_file or self.cache_answers
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise RuntimeError(f"refusing to overwrite symlink: {target}")
        payload = json.dumps(
            {
                "version": STATE_VERSION,
                "answers": {
                    key: value
                    for key, value in self.answers.items()
                    if key not in SENSITIVE_ANSWER_KEYS
                },
            },
            indent=2,
            sort_keys=True,
        )
        self.write(target, payload + "\n", 0o600)

    def remember(self, key: str, value: object) -> object:
        self.answers[key] = value
        if key not in SENSITIVE_ANSWER_KEYS:
            self.save_answers()
        return value

    def default_for(self, key: str, fallback: str | None = None) -> str | None:
        value = self.answers.get(key)
        if isinstance(value, str):
            return value
        return fallback

    def list_default_for(
        self, key: str, fallback: list[str] | None = None
    ) -> list[str] | None:
        value = self.answers.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        return fallback

    def prompt(
        self,
        text: str,
        default: str | None = None,
        secret: bool = False,
        allow_empty: bool = False,
    ) -> str:
        suffix = (
            " [saved]" if secret and default else f" [{default}]" if default else ""
        )
        while True:
            answer = (
                getpass.getpass(f"{text}{suffix}: ")
                if secret
                else input(f"{text}{suffix}: ")
            )
            answer = answer.strip()
            if answer or default is not None or allow_empty:
                if answer:
                    return answer
                if default is not None:
                    return default
                return ""

    def choose(self, title: str, options: dict[str, str], default: str) -> str:
        self.say(title)
        for key, description in options.items():
            self.say(f"  {key}) {description}")
        while (answer := self.prompt("Choice", default)) not in options:
            self.say("Please select one of the listed choices.")
        return answer

    def prompt_saved(
        self,
        key: str,
        text: str,
        default: str | None = None,
        secret: bool = False,
        allow_empty: bool = False,
        persist: bool = True,
    ) -> str:
        saved = self.answers.get(key)
        if isinstance(saved, str) and (saved or allow_empty):
            return saved
        answer = self.prompt(text, self.default_for(key, default), secret, allow_empty)
        if persist:
            self.remember(key, answer)
        else:
            self.answers[key] = answer
        return answer

    def choose_saved(
        self, key: str, title: str, options: dict[str, str], default: str
    ) -> str:
        saved = self.answers.get(key)
        if isinstance(saved, str) and saved in options:
            return saved
        saved_default = self.default_for(key, default) or default
        if saved_default not in options:
            saved_default = default
        answer = self.choose(title, options, saved_default)
        self.remember(key, answer)
        return answer

    @staticmethod
    def package_manager() -> str:
        for manager in ("apt", "dnf", "yum", "zypper", "pacman", "apk"):
            if COMMAND_PATHS.get(manager):
                return manager
        return "unknown"

    @staticmethod
    def packages_for(manager: str, missing: list[str]) -> list[str]:
        package_map = {
            "apt": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "ninja-build",
                "LWP::Protocol::https": "liblwp-protocol-https-perl",
                "Mozilla::CA": "libmozilla-ca-perl",
            },
            "dnf": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "ninja-build",
                "LWP::Protocol::https": "perl-LWP-Protocol-https",
                "Mozilla::CA": "perl-Mozilla-CA",
            },
            "yum": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "ninja-build",
                "LWP::Protocol::https": "perl-LWP-Protocol-https",
                "Mozilla::CA": "perl-Mozilla-CA",
            },
            "zypper": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "ninja",
                "LWP::Protocol::https": "perl-LWP-Protocol-https",
                "Mozilla::CA": "perl-Mozilla-CA",
            },
            "pacman": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "ninja",
                "LWP::Protocol::https": "perl-lwp-protocol-https",
                "Mozilla::CA": "perl-mozilla-ca",
            },
            "apk": {
                "perl": "perl",
                "git": "git",
                "make": "make",
                "compiler": "gcc",
                "bison": "bison",
                "flex": "flex",
                "tar": "tar",
                "gzip": "gzip",
                "systemctl": "systemd",
                "meson": "meson",
                "ninja": "samurai",
                "LWP::Protocol::https": "perl-lwp-protocol-https",
                "Mozilla::CA": "perl-mozilla-ca",
            },
        }
        selected = package_map.get(manager, {})
        packages = [selected.get(item, item) for item in missing]
        return sorted(dict.fromkeys(packages))

    @staticmethod
    def install_command(manager: str, packages: list[str]) -> str:
        if manager == "apt":
            return "sudo apt install " + " ".join(packages)
        if manager in {"dnf", "yum"}:
            return f"sudo {manager} install " + " ".join(packages)
        if manager == "zypper":
            return "sudo zypper install " + " ".join(packages)
        if manager == "pacman":
            return "sudo pacman -S " + " ".join(packages)
        if manager == "apk":
            return "sudo apk add " + " ".join(packages)
        return "Install missing packages with your OS package manager: " + " ".join(
            packages
        )

    def perl_module_available(self, module: str) -> bool:
        perl = self.command_path("perl")
        if perl is None:
            return False
        result = subprocess.run(
            [perl, f"-M{module}", "-e1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def dependency_check(self) -> list[str]:
        missing: list[str] = []
        command_checks = {
            "perl": "perl",
            "git": "git",
            "compiler": "compiler",
            "bison": "bison",
            "flex": "flex",
            "tar": "tar",
            "gzip": "gzip",
            "systemctl": "systemctl",
        }
        for label, command_label in command_checks.items():
            if not self.command_available(command_label):
                missing.append(label)
        for module in ("LWP::Protocol::https", "Mozilla::CA"):
            if not self.perl_module_available(module):
                missing.append(module)
        if missing:
            self.report_missing(missing)
        else:
            self.say("Required command-line prerequisites are available.")
        return missing

    def report_missing(
        self, missing: list[str], label: str = "Missing prerequisites"
    ) -> None:
        manager = self.package_manager()
        packages = self.packages_for(manager, missing)
        self.say(label + ": " + ", ".join(missing))
        self.say(
            "Proposed install command (not run): "
            + self.install_command(manager, packages)
        )

    @staticmethod
    def command_available(command_label: str) -> bool:
        return COMMAND_PATHS.get(command_label) is not None

    def build_system_dependency_check(self, build_system: str) -> list[str]:
        missing: list[str] = []
        if build_system == "make":
            if not self.command_available("make"):
                missing.append("make")
        elif build_system == "meson":
            if not self.command_available("meson"):
                missing.append("meson")
            if not self.command_available("ninja"):
                missing.append("ninja")
        return missing

    @staticmethod
    def build_system_label(build_system: str) -> str:
        if build_system == "meson":
            return "Meson"
        return "make/configure"

    def report_build_system_dependency_check(
        self, build_system: str, *, report_available: bool = False
    ) -> list[str]:
        missing = self.build_system_dependency_check(build_system)
        label = self.build_system_label(build_system)
        if missing:
            self.report_missing(
                missing,
                label=f"Missing {label} build-system prerequisites",
            )
        elif report_available:
            self.say(f"{label} build-system prerequisites are available.")
        return missing

    def check_only_dependency_check(self) -> list[str]:
        missing = self.dependency_check()
        for build_system in ("make", "meson"):
            missing.extend(
                self.report_build_system_dependency_check(
                    build_system,
                    report_available=True,
                )
            )
        return sorted(dict.fromkeys(missing))

    @staticmethod
    def branch_key(branch: str) -> tuple[int, int]:
        if branch == "HEAD":
            return (10_000, 0)
        match = re.fullmatch(r"REL_(\d+)_STABLE", branch)
        if match:
            return (int(match.group(1)), 0)
        return (-1, 0)

    def discover_branches(self, remote: str) -> list[str]:
        git = self.command_path("git")
        if git is None or self.args.dry_run:
            return DEFAULT_BRANCHES
        try:
            result = subprocess.run(
                [git, "ls-remote", "--heads", remote],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
            )
            found = {
                line.rsplit("/", 1)[-1]
                for line in result.stdout.splitlines()
                if "refs/heads/" in line
            }
            stable = sorted(
                (b for b in found if BRANCH_RE.fullmatch(b) and b != "HEAD"),
                key=self.branch_key,
                reverse=True,
            )
            available = ["HEAD", *stable] if stable else DEFAULT_BRANCHES
            return available
        except (subprocess.SubprocessError, OSError):
            self.say(
                "Could not discover remote branches; using the built-in branch list."
            )
            return DEFAULT_BRANCHES

    def default_branches_from(self, available: list[str]) -> list[str]:
        available_set = set(available)
        missing = [branch for branch in DEFAULT_BRANCHES if branch not in available_set]
        if missing:
            self.say(
                "Default branches missing from discovery and kept from fallback: "
                + ", ".join(missing)
            )
        return DEFAULT_BRANCHES.copy()

    def latest_branches_from(self, available: list[str], count: int) -> list[str]:
        stable = [
            branch
            for branch in available
            if branch != "HEAD" and BRANCH_RE.fullmatch(branch)
        ]
        stable = sorted(stable, key=self.branch_key, reverse=True)
        return ["HEAD", *stable[:count]]

    def parse_custom_branches(self) -> list[str]:
        branches = [x.strip() for x in self.prompt("Branches").split(",") if x.strip()]
        invalid = [branch for branch in branches if not BRANCH_RE.fullmatch(branch)]
        if invalid:
            raise RuntimeError("invalid branch name(s): " + ", ".join(invalid))
        if not branches:
            raise RuntimeError("at least one branch is required")
        return branches

    def choose_option_preset(
        self,
        title: str,
        presets: dict[str, tuple[str, list[str]]],
        choice_key: str,
        custom_key: str,
        values_key: str,
    ) -> list[str]:
        options = {
            **{
                key: f"{label}: {' '.join(values)}"
                for key, (label, values) in presets.items()
            },
            "3": "custom options",
        }
        choice = self.choose_saved(choice_key, title, options, "1")
        if choice == "3":
            saved = self.list_default_for(values_key)
            if saved is not None:
                values = saved
            else:
                values = shlex.split(self.prompt_saved(custom_key, "Options"))
        else:
            values = presets[choice][1].copy()
        self.remember(values_key, values)
        return values

    def collect(self) -> Choices:
        default_root = self.home / "pg-buildfarm"
        path_choice = self.choose_saved(
            "path_choice",
            "Installation path:",
            {"1": "~/pg-buildfarm", "2": "/opt/pg-buildfarm", "3": "custom path"},
            "1",
        )
        root = (
            default_root
            if path_choice == "1"
            else (
                Path("/opt/pg-buildfarm")
                if path_choice == "2"
                else Path(self.prompt_saved("custom_root", "Installation path"))
            )
        )
        root = root.expanduser().resolve()
        self.remember("root", str(root))
        if root == Path("/opt/pg-buildfarm") and not os.access(root.parent, os.W_OK):
            self.say(
                "/opt/pg-buildfarm requires write permission to /opt. No sudo will be used."
            )
        client_source = self.choose_saved(
            "client_source",
            "Buildfarm client source:",
            {
                "1": "download latest-client.tgz",
                "2": "clone client-code from GitHub",
                "3": "copy an existing local client directory",
            },
            "1",
        )
        client_local_path = None
        if client_source == "3":
            client_local_path = (
                Path(
                    self.prompt_saved(
                        "client_local_path", "Local buildfarm client directory"
                    )
                )
                .expanduser()
                .resolve()
            )
        remote_choice = self.choose_saved(
            "remote_choice",
            "PostgreSQL repository mirror:",
            {"1": PG_REMOTES["1"], "2": PG_REMOTES["2"]},
            "1",
        )
        remote = PG_REMOTES[remote_choice]
        self.remember("pg_remote", remote)
        mirror_mode = self.choose_saved(
            "mirror_mode",
            "Mirror mode:",
            {"1": "buildfarm-managed mirror", "2": "user-maintained local bare mirror"},
            "1",
        )
        discovered = self.discover_branches(remote)
        default_branches = self.default_branches_from(discovered)
        self.say("Discovered branches: " + ", ".join(discovered))
        self.say("Default selected branches: " + ", ".join(default_branches))
        branch_choice = self.choose_saved(
            "branch_choice",
            "Branch list:",
            {
                "1": "use default HEAD + REL_19_STABLE..REL_16_STABLE",
                "2": "latest N discovered stable branches plus HEAD",
                "3": "enter a custom comma-separated list",
            },
            "1",
        )
        if branch_choice == "1":
            branches = default_branches
        elif branch_choice == "2":
            count_text = self.prompt_saved(
                "branch_count", "Number of stable branches after HEAD", "4"
            )
            count = int(count_text)
            if count < 1:
                raise RuntimeError("branch count must be positive")
            branches = self.latest_branches_from(discovered, count)
        else:
            saved_branches = self.list_default_for("branches")
            if saved_branches is not None:
                branches = saved_branches
            else:
                branches = [
                    x.strip()
                    for x in self.prompt_saved("custom_branches", "Branches").split(",")
                    if x.strip()
                ]
            invalid = [branch for branch in branches if not BRANCH_RE.fullmatch(branch)]
            if invalid:
                raise RuntimeError("invalid branch name(s): " + ", ".join(invalid))
            if not branches:
                raise RuntimeError("at least one branch is required")
        self.remember("branches", branches)
        schedule = self.choose_saved(
            "schedule",
            "Build schedule:",
            {
                "1": "daily",
                "2": "twice daily",
                "3": "four times daily",
                "4": "custom systemd OnCalendar",
            },
            "2",
        )
        calendars = {
            "1": "*-*-* 03:00:00",
            "2": "*-*-* 00,12:00:00",
            "3": "*-*-* 00,06,12,18:00:00",
        }
        calendar = calendars.get(schedule) or self.prompt_saved(
            "custom_calendar", "OnCalendar expression"
        )
        self.validate_calendar(calendar)
        self.remember("calendar", calendar)
        animal = self.prompt_saved(
            "animal",
            "Buildfarm animal name (blank allowed before registration)",
            allow_empty=True,
        )
        secret = self.prompt_saved(
            "secret",
            "Buildfarm secret (blank allowed before registration)",
            secret=True,
            allow_empty=True,
            persist=False,
        )
        build_system_choice = self.choose_saved(
            "build_system_choice",
            "PostgreSQL build system:",
            {"1": "make / configure", "2": "Meson"},
            "2",
        )
        build_system = "meson" if build_system_choice == "2" else "make"
        self.remember("build_system", build_system)
        make = self.command_path("make") or "make"
        config_opts: list[str] = []
        meson_opts: list[str] = []
        meson_jobs = ""
        meson_test_timeout = ""
        if build_system == "make":
            make = self.prompt_saved("make", "make command", make)
            config_opts = self.choose_option_preset(
                "configure option preset:",
                MAKE_PRESETS,
                "config_preset",
                "custom_config_opts",
                "config_opts",
            )
        else:
            meson_opts = self.choose_option_preset(
                "Meson option preset:",
                MESON_PRESETS,
                "meson_preset",
                "custom_meson_opts",
                "meson_opts",
            )
            meson_jobs = self.prompt_saved(
                "meson_jobs", "Meson jobs (blank for Meson default)", allow_empty=True
            )
            meson_jobs = self.normalize_meson_jobs(meson_jobs)
            meson_test_timeout = self.prompt_saved(
                "meson_test_timeout", "Meson test timeout multiplier", "3"
            )
            meson_test_timeout = self.normalize_meson_test_timeout(meson_test_timeout)
        extra_path = self.prompt_saved(
            "extra_path",
            "Extra PATH prefix for build_env (blank for none)",
            allow_empty=True,
        )
        return Choices(
            root,
            client_source,
            client_local_path,
            remote,
            mirror_mode,
            branches,
            calendar,
            animal,
            secret,
            build_system,
            make,
            config_opts,
            meson_opts,
            meson_jobs,
            meson_test_timeout,
            extra_path,
        )

    @staticmethod
    def perl(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def validate_calendar(self, calendar: str) -> None:
        if "\n" in calendar or "\r" in calendar:
            raise RuntimeError("systemd OnCalendar expression cannot contain newlines")
        analyzer = self.command_path("systemd-analyze")
        if analyzer is None:
            self.say(
                "Cannot validate OnCalendar expression: systemd-analyze is unavailable."
            )
            return
        result = subprocess.run(
            [analyzer, "calendar", calendar],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            message = result.stderr.strip() or calendar
            raise RuntimeError(f"invalid systemd OnCalendar expression: {message}")

    def perl_list_items(self, values: list[str]) -> str:
        return ", ".join(self.perl(value) for value in values)

    def perl_array_with_commented_extras(
        self, name: str, values: list[str], extra_values: list[str]
    ) -> str:
        lines = [f"my @{name} = ("]
        lines.extend(f"    {self.perl(value)}," for value in values)
        if extra_values:
            lines.append("")
            lines.append("    # Additional Linux testing options. Uncomment as needed.")
            lines.extend(f"    # {self.perl(value)}," for value in extra_values)
        lines.append(");")
        return "\n".join(lines)

    @staticmethod
    def branch_helper() -> str:
        return """my $branch;
{
    no warnings 'once';
    $branch = defined $main::branch ? $main::branch : 'HEAD';
}

sub pg_major {
    return 999 if !defined $branch || $branch eq 'HEAD';
    return $1  if $branch =~ /^REL_(\\d+)_STABLE$/;
    return 0;
}

my $pg_major = pg_major();
"""

    @staticmethod
    def make_branch_option_comments() -> str:
        return """# Additional branch-specific configure options. Uncomment as needed.
# push @config_opts, '--enable-injection-points'
#   if $pg_major >= 17;
#
# push @config_opts, qw(
#   --with-libcurl
#   --with-libnuma
#   --with-liburing
# ) if $pg_major >= 18;
"""

    @staticmethod
    def meson_branch_option_comments() -> str:
        return """# Additional branch-specific Meson options. Uncomment as needed.
# push @meson_opts, '-Dinjection_points=true'
#   if $pg_major >= 17;
#
# push @meson_opts, qw(
#   -Dlibcurl=enabled
#   -Dlibnuma=enabled
#   -Dliburing=enabled
# ) if $pg_major >= 18;
"""

    def environment_variables(self, extra_path: str) -> str:
        lines = [
            "my %config_env;",
            "",
            "# Additional compiler stress/debug options. Uncomment as needed.",
            "# my @cppflags = qw(",
        ]
        lines.extend(f"#   {value}" for value in CPPFLAGS_EXTRA_OPTIONS)
        lines.extend(
            [
                "# );",
                "# push @cppflags, '-DREALLOCATE_BITMAPSETS'",
                "#   if $pg_major >= 17;",
                "# $config_env{CPPFLAGS} = join( ' ', @cppflags );",
                "",
                "my %build_env;",
            ]
        )
        if extra_path:
            lines.append(
                "$build_env{PATH} = "
                + self.perl(extra_path)
                + " . ':' . $ENV{PATH};"
            )
        lines.extend(
            [
                "",
                "# Additional TAP test groups. Uncomment as needed.",
                "# my @pg_test_extra = qw(",
            ]
        )
        lines.extend(f"#   {value}" for value in PG_TEST_EXTRA_OPTIONS)
        lines.extend(
            [
                "# );",
                "# push @pg_test_extra, qw(",
                "#   libpq_encryption",
                "#   xid_wraparound",
                "# ) if $pg_major >= 17;",
                "# push @pg_test_extra, qw(",
                "#   oauth",
                "#   regress_dump_restore",
                "# ) if $pg_major >= 18;",
                "# push @pg_test_extra, qw(",
                "#   checksum",
                "#   checksum_extended",
                "#   saslprep",
                "# ) if $pg_major >= 19;",
                "# $build_env{PG_TEST_EXTRA} = join( ' ', @pg_test_extra );",
            ]
        )
        return "\n".join(lines)

    def make_build_variables(self, c: Choices) -> str:
        return "\n\n".join(
            [
                self.branch_helper(),
                self.perl_array_with_commented_extras(
                    "config_opts", c.config_opts, MAKE_EXTRA_OPTIONS
                ),
                self.make_branch_option_comments(),
                self.environment_variables(c.extra_path),
            ]
        )

    def meson_build_variables(self, c: Choices) -> str:
        return "\n\n".join(
            [
                self.branch_helper(),
                self.perl_array_with_commented_extras(
                    "meson_opts", c.meson_opts, MESON_EXTRA_OPTIONS
                ),
                self.meson_branch_option_comments(),
                self.environment_variables(c.extra_path),
            ]
        )

    @staticmethod
    def normalize_meson_jobs(value: str) -> str:
        if not value:
            return ""
        if re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise RuntimeError("Meson jobs must be a positive integer or blank")
        return value

    @staticmethod
    def normalize_meson_test_timeout(value: str) -> str:
        if re.fullmatch(r"[0-9]+", value) is None:
            raise RuntimeError(
                "Meson test timeout multiplier must be a non-negative integer"
            )
        return value.lstrip("0") or "0"

    def scm_url_for(self, remote: str) -> str:
        if "github.com/postgres/postgres" in remote:
            return "https://github.com/postgres/postgres/commit/"
        return ""

    def config(self, c: Choices, client_dir: Path, mirror: Path) -> str:
        scmrepo = str(mirror) if c.mirror_mode == "2" else c.pg_remote
        scm_url = self.scm_url_for(c.pg_remote)
        mirror_settings = ""
        if c.mirror_mode == "1":
            mirror_settings = (
                "    git_keep_mirror => 1,\n    git_ignore_mirror_failure => 1,\n"
            )
        branches = self.perl_list_items(c.branches)
        if c.build_system == "meson":
            build_variables = self.meson_build_variables(c)
            meson_jobs = self.normalize_meson_jobs(c.meson_jobs) or "undef"
            meson_test_timeout = self.normalize_meson_test_timeout(c.meson_test_timeout)
            build_system_settings = f"""    using_meson => 1,
    meson_jobs => {meson_jobs},
    meson_test_timeout => {meson_test_timeout},
    meson_opts => \\@meson_opts,"""
        else:
            build_variables = self.make_build_variables(c)
            build_system_settings = f"""    using_meson => 0,
    make => {self.perl(c.make)},
    config_opts => \\@config_opts,"""
        return f"""# Generated by pg_buildfarm_installer.py; contains a secret: mode 0600.
# -*-perl-*-
package PGBuild;

use strict;
use warnings FATAL => 'qw';

our (%conf);

{build_variables}
%conf = (
    animal => {self.perl(c.animal)},
    secret => {self.perl(c.secret)},
    scm => 'git',
    scmrepo => {self.perl(scmrepo)},
    scm_url => {self.perl(scm_url) if scm_url else 'undef'},
    target => {self.perl(BUILDFARM_TARGET)},
    upgrade_target => {self.perl(BUILDFARM_UPGRADE_TARGET)},
{mirror_settings}    git_use_workdirs => 1,
    rm_worktrees => 1,
    build_root => {self.perl(str(c.root / 'builds'))},
{build_system_settings}
    config_env => \\%config_env,
    build_env => \\%build_env,
    global => {{
        branches_to_build => [ {branches} ],
    }},
);

1;
"""

    @staticmethod
    def expected_client_scripts(client_dir: Path) -> list[Path]:
        return [
            client_dir / name
            for name in ("run_branches.pl", "run_build.pl", "run_web_txn.pl")
        ]

    def verify_client_dir(self, client_dir: Path) -> None:
        try:
            root_mode = client_dir.lstat().st_mode
        except FileNotFoundError:
            root_mode = None
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect client path {client_dir}: {exc}"
            ) from exc
        if root_mode is not None and stat.S_ISLNK(root_mode):
            raise RuntimeError(f"client directory contains symlink: {client_dir}")
        missing = [
            path.name
            for path in self.expected_client_scripts(client_dir)
            if not path.exists()
        ]
        if missing:
            raise RuntimeError(
                f"client directory {client_dir} is missing: {', '.join(missing)}"
            )
        unsafe = [
            path.name
            for path in self.expected_client_scripts(client_dir)
            if path.is_symlink() or not path.is_file()
        ]
        if unsafe:
            raise RuntimeError(
                f"client directory {client_dir} has unsafe script file(s): "
                + ", ".join(unsafe)
            )
        self.verify_client_tree(client_dir)

    @staticmethod
    def client_tree_paths(client_dir: Path) -> list[Path]:
        paths: list[Path] = []
        pending = [client_dir]
        while pending:
            path = pending.pop()
            paths.append(path)
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise RuntimeError(f"cannot inspect client path {path}: {exc}") from exc
            if stat.S_ISDIR(mode):
                try:
                    with os.scandir(path) as entries:
                        pending.extend(Path(entry.path) for entry in entries)
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot inspect client directory {path}: {exc}"
                    ) from exc
        return paths

    def verify_client_tree(self, client_dir: Path) -> None:
        current_uid = os.geteuid()
        for path in self.client_tree_paths(client_dir):
            try:
                status = path.lstat()
            except OSError as exc:
                raise RuntimeError(f"cannot inspect client path {path}: {exc}") from exc
            mode = status.st_mode
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"client directory contains symlink: {path}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeError(
                    f"client directory contains unsupported file type: {path}"
                )
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError(
                    f"client directory contains group/world-writable path: {path}"
                )
            owner = getattr(status, "st_uid", current_uid)
            if owner not in (current_uid, 0):
                raise RuntimeError(
                    f"client directory contains path not owned by the current user or root: {path}"
                )

    def verify_local_client_source(self, client_dir: Path) -> None:
        self.verify_client_dir(client_dir)

    def harden_client_permissions(self, client_dir: Path) -> None:
        for path in self.client_tree_paths(client_dir):
            try:
                status = path.lstat()
            except OSError as exc:
                raise RuntimeError(f"cannot inspect client path {path}: {exc}") from exc
            mode = status.st_mode
            if stat.S_ISLNK(mode):
                continue
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                safe_mode = stat.S_IMODE(mode) & ~(stat.S_IWGRP | stat.S_IWOTH)
                if safe_mode != stat.S_IMODE(mode):
                    try:
                        path.chmod(safe_mode)
                    except OSError as exc:
                        raise RuntimeError(
                            f"cannot harden client path permissions {path}: {exc}"
                        ) from exc

    @staticmethod
    @contextlib.contextmanager
    def restricted_install_umask():
        previous = os.umask(stat.S_IWGRP | stat.S_IWOTH)
        os.umask(previous | stat.S_IWGRP | stat.S_IWOTH)
        try:
            yield
        finally:
            os.umask(previous)

    @staticmethod
    def validate_tar_member(member: tarfile.TarInfo) -> None:
        name = Path(member.name)
        if (
            name.is_absolute()
            or ".." in name.parts
            or not (member.isdir() or member.isreg())
        ):
            raise RuntimeError(f"unsafe tar member rejected: {member.name}")

    @classmethod
    def validate_tar_members(cls, members: list[tarfile.TarInfo]) -> None:
        if len(members) > MAX_CLIENT_ARCHIVE_MEMBERS:
            raise RuntimeError("client archive has too many members")
        total_size = 0
        for member in members:
            cls.validate_tar_member(member)
            if member.isreg():
                if member.size > MAX_CLIENT_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(
                        f"client archive member is too large: {member.name}"
                    )
                total_size += member.size
                if total_size > MAX_CLIENT_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("client archive expands to too much data")

    @staticmethod
    def publish_staged_client(staged_client: Path, client_dir: Path) -> None:
        try:
            staged_client.rename(client_dir)
        except OSError as exc:
            raise RuntimeError(
                f"unable to publish staged client directory {client_dir}: {exc}"
            ) from exc

    def download_client_archive(self, archive: Path) -> None:
        try:
            with (
                urllib.request.urlopen(CLIENT_TARBALL) as response,
                archive.open("wb") as output,
            ):
                content_length = response.headers.get("Content-Length")
                if (
                    content_length
                    and content_length.isascii()
                    and content_length.isdigit()
                ):
                    if int(content_length) > MAX_CLIENT_ARCHIVE_BYTES:
                        raise RuntimeError("client archive download is too large")
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_CLIENT_ARCHIVE_BYTES:
                        raise RuntimeError("client archive download is too large")
                    output.write(chunk)
        except RuntimeError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"unable to download buildfarm client: {exc}") from exc

    def install_client(self, c: Choices, client_dir: Path) -> None:
        if os.path.lexists(client_dir):
            self.verify_client_dir(client_dir)
            self.say(
                f"Client directory already exists and looks usable: {client_dir}; leaving it in place."
            )
            return
        if self.args.dry_run:
            if c.client_source == "2":
                git = self.command_path("git") or "git"
                self.say(f"DRY-RUN: create {c.root}")
                self.run([git, "clone", CLIENT_GIT, str(client_dir)])
            elif c.client_source == "3":
                self.say(
                    f"DRY-RUN: copy local buildfarm client from {c.client_local_path} to {client_dir}"
                )
            else:
                self.say(f"DRY-RUN: create {c.root}")
                self.say(
                    f"DRY-RUN: download and safely extract {CLIENT_TARBALL} into {client_dir}"
                )
            return
        c.root.mkdir(parents=True, exist_ok=True)
        try:
            with self.restricted_install_umask():
                with tempfile.TemporaryDirectory(
                    prefix=".client-staging-", dir=c.root
                ) as temp:
                    staging = Path(temp)
                    staged_client = staging / "client"
                    if c.client_source == "2":
                        git = self.require_command("git")
                        try:
                            self.run([git, "clone", CLIENT_GIT, str(staged_client)])
                        except (OSError, subprocess.CalledProcessError) as exc:
                            raise RuntimeError(
                                f"unable to clone buildfarm client: {exc}"
                            ) from exc
                    elif c.client_source == "3":
                        if c.client_local_path is None:
                            raise RuntimeError(
                                "local buildfarm client directory was not provided"
                            )
                        self.verify_local_client_source(c.client_local_path)
                        try:
                            shutil.copytree(
                                c.client_local_path, staged_client, symlinks=True
                            )
                        except (OSError, shutil.Error) as exc:
                            raise RuntimeError(
                                f"unable to copy local buildfarm client: {exc}"
                            ) from exc
                    else:
                        archive = staging / "client.tgz"
                        extract_root = staging / "extract"
                        extract_root.mkdir()
                        self.download_client_archive(archive)
                        try:
                            with tarfile.open(archive, "r:gz") as tar:
                                members = tar.getmembers()
                                self.validate_tar_members(members)
                                tar.extractall(
                                    extract_root, members=members, filter="data"
                                )
                        except (OSError, tarfile.TarError) as exc:
                            raise RuntimeError(
                                f"unable to extract buildfarm client archive: {exc}"
                            ) from exc
                        entries = list(extract_root.iterdir())
                        source = (
                            entries[0]
                            if len(entries) == 1 and entries[0].is_dir()
                            else extract_root
                        )
                        if source == extract_root:
                            staged_client.mkdir()
                            for entry in entries:
                                entry.rename(staged_client / entry.name)
                        else:
                            source.rename(staged_client)
                    self.harden_client_permissions(staged_client)
                    self.verify_client_dir(staged_client)
                    self.publish_staged_client(staged_client, client_dir)
        except shutil.Error as exc:
            raise RuntimeError(f"unable to install buildfarm client: {exc}") from exc

    def validate(self, config: Path, client_dir: Path) -> bool:
        targets = [config] + [
            p for p in self.expected_client_scripts(client_dir) if p.exists()
        ]
        ok = True
        perl = self.command_path("perl")
        if perl is None:
            self.say("Cannot validate: perl is unavailable.")
            return False
        for target in targets:
            try:
                result = self.run([perl, "-cw", str(target)], check=False)
                if result and result.returncode:
                    self.say(f"Validation failed: perl -cw {target}")
                    ok = False
            except FileNotFoundError:
                self.say(f"Cannot validate: {perl} is unavailable.")
                return False
        if not self.args.dry_run:
            missing = [
                path
                for path in self.expected_client_scripts(client_dir)
                if not path.exists()
            ]
            if missing:
                self.say(
                    "Validation failed: missing client script(s): "
                    + ", ".join(str(path) for path in missing)
                )
                ok = False
        return ok

    def verify_existing_mirror(self, mirror: Path, remote: str) -> None:
        if not mirror.is_dir():
            raise RuntimeError(f"existing mirror path is not a directory: {mirror}")
        git = self.require_command("git")
        bare = subprocess.run(
            [git, "-C", str(mirror), "rev-parse", "--is-bare-repository"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if bare.returncode or bare.stdout.strip() != "true":
            raise RuntimeError(
                f"existing mirror path is not a bare git repository: {mirror}"
            )
        origin = subprocess.run(
            [git, "-C", str(mirror), "config", "--get", "remote.origin.url"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if origin.returncode:
            raise RuntimeError(f"existing mirror has no remote.origin.url: {mirror}")
        if origin.stdout.strip() != remote:
            raise RuntimeError(
                f"existing mirror remote is {origin.stdout.strip()!r}, expected {remote!r}"
            )

    @staticmethod
    def systemd_escape(value: str) -> str:
        if "\n" in value:
            raise RuntimeError("systemd unit values cannot contain newlines")
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("%", "%%")
            .replace("$", "$$")
        )

    @classmethod
    def systemd_arg(cls, value: str | Path) -> str:
        return '"' + cls.systemd_escape(str(value)) + '"'

    @classmethod
    def systemd_path(cls, value: str | Path) -> str:
        path = str(value)
        if not Path(path).is_absolute():
            raise RuntimeError(f"systemd path values must be absolute: {path}")
        if "\n" in path:
            raise RuntimeError("systemd unit values cannot contain newlines")
        return (
            path.replace("\\", "\\\\")
            .replace(" ", "\\x20")
            .replace("\t", "\\t")
            .replace('"', '\\"')
            .replace("%", "%%")
        )

    def write_units(
        self, c: Choices, client_dir: Path, config: Path, mirror: Path
    ) -> None:
        perl = self.command_path("perl") or "/usr/bin/perl"
        git = self.command_path("git") or "/usr/bin/git"
        buildfarm_args = [
            perl,
            "./run_branches.pl",
            "--run-all",
            "--nosend",
            "--nostatus",
            "--verbose",
            "--config",
            config,
        ]
        service = (
            "[Unit]\nDescription=PostgreSQL Buildfarm commissioning run\n\n"
            "[Service]\nType=oneshot\n"
            f"WorkingDirectory={self.systemd_path(client_dir)}\n"
            "ExecStart="
            + " ".join(self.systemd_arg(arg) for arg in buildfarm_args)
            + "\n"
        )
        timer = f"""[Unit]\nDescription=Schedule PostgreSQL Buildfarm commissioning runs\n\n[Timer]\nOnCalendar={c.calendar}\nPersistent=true\nUnit=pg-buildfarm.service\n\n[Install]\nWantedBy=timers.target\n"""
        self.write(self.unit_dir / "pg-buildfarm.service", service)
        self.write(self.unit_dir / "pg-buildfarm.timer", timer)
        if c.mirror_mode == "2":
            mirror_args = [git, "-C", mirror, "remote", "update", "--prune"]
            mirror_service = (
                "[Unit]\nDescription=Update PostgreSQL Buildfarm bare mirror\n\n"
                "[Service]\nType=oneshot\n"
                "ExecStart="
                + " ".join(self.systemd_arg(arg) for arg in mirror_args)
                + "\n"
            )
            mirror_timer = f"""[Unit]\nDescription=Schedule PostgreSQL Buildfarm mirror updates\n\n[Timer]\nOnCalendar=*-*-* 01,13:00:00\nPersistent=true\nUnit=pg-buildfarm-mirror-update.service\n\n[Install]\nWantedBy=timers.target\n"""
            self.write(
                self.unit_dir / "pg-buildfarm-mirror-update.service", mirror_service
            )
            self.write(self.unit_dir / "pg-buildfarm-mirror-update.timer", mirror_timer)

    @staticmethod
    def os_release_fields(path: Path = Path("/etc/os-release")) -> dict[str, str]:
        fields: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return fields
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key] = value.strip().strip("\"'")
        return fields

    def compiler_registration_info(self) -> tuple[str, str]:
        compiler = self.command_path("compiler")
        if compiler is None:
            return ("not found on PATH", "not found on PATH")
        compiler_name = Path(compiler).name
        compiler_family = self.compiler_family_from_macros(compiler)
        try:
            result = subprocess.run(
                [compiler, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return (compiler_name, "unable to run compiler --version")
        version_output = (result.stdout or result.stderr).strip()
        output = version_output.splitlines()
        compiler_version = output[0].strip() if output else "unknown"
        if compiler_family is None:
            compiler_family = self.compiler_family_from_version(version_output)
        return (
            self.compiler_registration_name(compiler_name, compiler_family),
            compiler_version,
        )

    @staticmethod
    def compiler_family_from_macros(compiler: str) -> str | None:
        try:
            result = subprocess.run(
                [compiler, "-dM", "-E", "-"],
                input="",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode:
            return None
        macros = result.stdout
        if "__clang__" in macros:
            return "clang"
        if "__GNUC__" in macros:
            return "gcc"
        return None

    @staticmethod
    def compiler_family_from_version(version_output: str) -> str | None:
        version_output_lower = version_output.lower()
        if "clang" in version_output_lower:
            return "clang"
        if (
            "gcc" in version_output_lower
            or "free software foundation" in version_output_lower
        ):
            return "gcc"
        return None

    @staticmethod
    def compiler_registration_name(
        command_name: str, compiler_family: str | None
    ) -> str:
        if compiler_family is None or compiler_family == command_name:
            return command_name
        return f"{compiler_family} (via {command_name})"

    def registration_system_info(self) -> list[tuple[str, str]]:
        os_release = self.os_release_fields()
        compiler_name, compiler_version = self.compiler_registration_info()
        return [
            (
                "Operating System",
                os_release.get("NAME") or platform.system() or "unknown",
            ),
            (
                "OS Version",
                os_release.get("VERSION")
                or os_release.get("VERSION_ID")
                or platform.release()
                or "unknown",
            ),
            ("Compiler", compiler_name),
            ("Compiler Version", compiler_version),
            ("Architecture", platform.machine() or "unknown"),
        ]

    def registration_system_info_text(self) -> str:
        return "\n".join(
            f"  {label}: {value}" for label, value in self.registration_system_info()
        )

    def print_next_steps(self, c: Choices, client_dir: Path, config: Path) -> None:
        service = self.unit_dir / "pg-buildfarm.service"
        self.say(
            "\nValidate the commissioning run before enabling the timer:\n"
            f"  cd {shlex.quote(str(client_dir))}\n"
            "  ./run_branches.pl --run-all --nosend --nostatus --verbose --config "
            f"{shlex.quote(str(config))}\n\n"
            "A successful validation ends with a zero exit status. To check it explicitly:\n"
            "  echo $?\n\n"
            "After a successful validation, load and enable the user timer:\n"
            "  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now pg-buildfarm.timer\n"
            "  systemctl --user status pg-buildfarm.timer\n\n"
            "To inspect the last scheduled run:\n"
            "  journalctl --user -u pg-buildfarm.service -n 200 --no-pager\n"
            "  systemctl --user show pg-buildfarm.service -p ExecMainStatus -p Result\n\n"
            "Use this system information when registering the animal:\n"
            f"{self.registration_system_info_text()}\n\n"
            "Register the animal, then add the approved animal name and secret to:\n"
            f"  {config}\n\n"
            "After credentials are added and a no-send run is clean, edit the service to remove:\n"
            "  --nosend --nostatus\n"
            f"from:\n  {service}\n"
        )
        if c.mirror_mode == "2":
            self.say(
                "For user-maintained local mirror mode, also enable the mirror update timer:\n"
                "  systemctl --user enable --now pg-buildfarm-mirror-update.timer\n"
            )

    def execute(self) -> int:
        if os.geteuid() == 0:
            self.say(
                "Refusing to run as root. Buildfarm must run as the existing login user."
            )
            return 2
        if self.args.check_only:
            missing = self.check_only_dependency_check()
            return 1 if missing else 0
        missing = self.dependency_check()
        if missing and not self.args.dry_run:
            self.say(
                "Installation stopped: install the listed prerequisites, then rerun this script."
            )
            return 1
        try:
            c = self.collect()
            client_dir, config, mirror = (
                c.root / "client",
                c.root / "build-farm.conf",
                c.root / "postgresql.git",
            )
            if c.root == Path("/opt/pg-buildfarm") and not os.access(
                c.root.parent, os.W_OK
            ):
                return 1
            build_system_missing = self.report_build_system_dependency_check(
                c.build_system
            )
            if build_system_missing:
                if not self.args.dry_run:
                    self.say(
                        "Installation stopped: install the listed build-system prerequisites, then rerun this script."
                    )
                    return 1
            self.install_client(c, client_dir)
            if not self.args.dry_run:
                (c.root / "builds").mkdir(parents=True, exist_ok=True)
            if c.mirror_mode == "2":
                if mirror.exists():
                    if not self.args.dry_run:
                        self.verify_existing_mirror(mirror, c.pg_remote)
                else:
                    git = self.require_command("git")
                    self.run([git, "clone", "--mirror", c.pg_remote, str(mirror)])
            self.write(config, self.config(c, client_dir, mirror), 0o600)
            self.write_units(c, client_dir, config, mirror)
            if not self.validate(config, client_dir):
                self.say(
                    "\nSetup completed with validation failures; fix them before enabling the timer."
                )
                return 1
        except (
            RuntimeError,
            ValueError,
            OSError,
            urllib.error.URLError,
            tarfile.TarError,
            shutil.Error,
            subprocess.CalledProcessError,
        ) as exc:
            self.say(f"Installation failed: {exc}")
            return 1
        self.say(
            "\nSetup complete." if not self.args.dry_run else "\nDry-run complete."
        )
        self.say(
            f"Client: {client_dir}\nConfig: {config} (secret not displayed)\nBuild system: {c.build_system}\nUnits: {self.unit_dir}"
        )
        self.print_next_steps(c, client_dir, config)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report prerequisite availability without changing anything",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show writes and commands without performing them",
    )
    parser.add_argument(
        "--answers-file",
        type=Path,
        help="read and update saved wizard answers from this JSON file",
    )
    parser.add_argument(
        "--reset-answers",
        action="store_true",
        help="ignore saved wizard answers for this run",
    )
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        print("Python 3.12 or later is required.", file=sys.stderr)
        return 2
    return Installer(args).execute()


if __name__ == "__main__":
    raise SystemExit(main())
