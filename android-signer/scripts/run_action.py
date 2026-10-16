#!/usr/bin/env python3
# Copyright 2026 Kenny Root
"""Pass composite inputs as literal arguments to the reviewed signing helper."""
import os
from pathlib import Path
import subprocess
import sys

OPTIONS = {
    "build-info": ("profile", "version-code-file"),
    "prepare": ("policy", "source-run", "source-attempt", "tools"),
    "seal": ("policy", "bundle"),
    "verify": ("policy", "manifest-sha256"),
    "github": ("policy", "manifest-sha256", "report"),
    "play": ("policy", "manifest-sha256", "report"),
}


def arguments(environment):
    command = environment.get("SIGNER_COMMAND", "")
    if command not in OPTIONS:
        raise ValueError("Unsupported Android signer operation")
    result = [command]
    for name in ("out", *OPTIONS[command]):
        value = environment.get("SIGNER_" + name.upper().replace("-", "_"), "")
        if not value:
            raise ValueError("Missing Android signer input: " + name)
        # '=' keeps values beginning with '--' from becoming additional options.
        result.append("--" + name + "=" + value)
    return result


def main():
    helper = Path(__file__).with_name("signer-publish.py")
    subprocess.run([sys.executable, str(helper), *arguments(os.environ)], check=True)


if __name__ == "__main__":
    main()
