#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        print(message, file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-all", action="store_true")
    parser.add_argument("--nosend", action="store_true")
    parser.add_argument("--nostatus", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    require(args.run_all, "--run-all is required")
    require(args.nosend, "--nosend is required")
    require(args.nostatus, "--nostatus is required")

    config = Path(args.config)
    require(config.exists(), f"config does not exist: {config}")
    text = config.read_text(encoding="utf-8")

    required_fragments = [
        "target => 'https://buildfarm.postgresql.org/cgi-bin/pgstatus.pl'",
        "upgrade_target => 'https://buildfarm.postgresql.org/cgi-bin/upgrade.pl'",
        "config_env => \\%config_env",
        "build_env => \\%build_env",
        "branches_to_build => [",
    ]
    for fragment in required_fragments:
        require(fragment in text, f"missing config fragment: {fragment}")

    expected_branches = os.environ.get("FAKE_EXPECTED_BRANCHES", "HEAD,REL_19_STABLE").split(",")
    for branch in expected_branches:
        require(f"'{branch}'" in text, f"missing expected branch: {branch}")

    expected_build_system = os.environ.get("FAKE_EXPECTED_BUILD_SYSTEM")
    if expected_build_system == "make":
        require("using_meson => 0" in text, "make config must disable Meson")
        require("config_opts => \\@config_opts" in text, "missing make configure options")
        require("my @config_opts = (" in text, "missing make configure option array")
        require("my @meson_opts" not in text, "make config must not define Meson options")
    elif expected_build_system == "meson":
        require("using_meson => 1" in text, "Meson config must enable Meson")
        require("meson_opts => \\@meson_opts" in text, "missing Meson options")
        require("my @meson_opts = (" in text, "missing Meson option array")
        require("my @config_opts" not in text, "Meson config must not define configure options")
        require("meson_jobs => 2" in text, "missing Meson jobs")
        require("meson_test_timeout => 3" in text, "missing Meson test timeout")

    print("FAKE_BUILDFARM_VALIDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
