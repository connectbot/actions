#!/usr/bin/env python3
# Copyright 2026 Kenny Root
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("action", ROOT / "scripts/run_action.py")
action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(action)


class ActionTests(unittest.TestCase):
    def test_every_operation_passes_only_its_declared_inputs(self):
        for command, options in action.OPTIONS.items():
            with self.subTest(command=command):
                env = {"SIGNER_COMMAND": command, "SIGNER_OUT": "/tmp/output"}
                env.update({"SIGNER_" + key.upper().replace("-", "_"): key for key in options})
                env["SIGNER_UNDECLARED"] = "ignored"
                self.assertEqual(action.arguments(env), [command, "--out=/tmp/output", *["--" + key + "=" + key for key in options]])

    def test_untrusted_values_remain_literal_arguments(self):
        value = "--report=/other path; $(touch /tmp/should-not-exist)\nsecond line"
        args = action.arguments({"SIGNER_COMMAND": "build-info", "SIGNER_OUT": value,
            "SIGNER_PROFILE": "oss", "SIGNER_VERSION_CODE_FILE": value})
        self.assertEqual(args, ["build-info", "--out=" + value, "--profile=oss", "--version-code-file=" + value])

    def test_unknown_operation_and_missing_inputs_are_rejected(self):
        for env in ({"SIGNER_COMMAND": "bash"}, {"SIGNER_COMMAND": "prepare", "SIGNER_OUT": "/tmp/output"}):
            with self.assertRaises(ValueError):
                action.arguments(env)


if __name__ == "__main__":
    unittest.main()
