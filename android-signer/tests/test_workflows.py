#!/usr/bin/env python3
# Copyright 2026 Kenny Root
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WorkflowTests(unittest.TestCase):
    def test_publishers_verify_before_google_authentication_and_only_target_alpha(self):
        workflow = (ROOT / ".github/workflows/android-publish.yml").read_text()
        self.assertLess(workflow.index("command: verify"), workflow.index("google-github-actions/auth@"))
        self.assertIn("project_id: api-4664730880327298161-873945", workflow)
        self.assertIn("github.repository_id == '123235'", workflow)
        self.assertIn("environment: play-alpha", workflow)
        self.assertNotIn("credentials_json", workflow)
        self.assertNotIn("attestations: write", workflow)
        self.assertNotIn("id-token: write", workflow.split("  play:")[0])
        self.assertNotIn("actions/checkout@", workflow)

    def attestor(self):
        return (ROOT / ".github/workflows/android-attest.yml").read_text()

    def python_step(self, name):
        step = self.attestor().split("      - name: " + name + "\n", 1)[1]
        code = step.split("<<'PYTHON'\n", 1)[1].split("          PYTHON\n", 1)[0]
        return textwrap.dedent(code)

    def test_attestor_only_accepts_profile_and_uses_platform_run_identity(self):
        workflow = self.attestor()
        interface, job = workflow.split("jobs:\n", 1)
        self.assertEqual(interface.count("        type: string"), 1)
        self.assertIn("      profile:", interface)
        for forbidden in ("actions/checkout@", "gradlew", "setup-java@", "setup-android@",
                          "setup-gradle@", "secrets:", "run-id:", "repository:",
                          "artifact-ids:", "pattern:", "merge-multiple:", "continue-on-error:"):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("runs-on: ubuntu-latest", job)
        self.assertIn("github.event_name == 'push'", job)
        for artifact in ("unsigned", "release-support", "provenance"):
            self.assertIn("name: " + artifact + "-${{ inputs.profile }}-${{ github.run_attempt }}", job)
        self.assertIn("support/build-info.json", job)
        self.assertLess(job.index("Validate artifact handoff"), job.index("uses: actions/attest@"))
        self.assertFalse((ROOT / ".github/workflows/android-build.yml").exists())

    def test_profile_rejects_paths_globs_and_shell_expressions(self):
        code = self.python_step("Validate artifact profile")
        for profile in ("oss", "google", "another-profile", "../oss", "*", "oss/extra",
                        "$(touch injected)", "oss\nextra", "", "a" * 33):
            with self.subTest(profile=profile):
                result = subprocess.run([sys.executable, "-c", code],
                    env={**os.environ, "PROFILE": profile}, capture_output=True)
                self.assertEqual(result.returncode == 0, profile in ("oss", "google", "another-profile"))

    def test_handoff_rejects_missing_extra_and_symlinked_files(self):
        code = self.python_step("Validate artifact handoff")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory, names in (("unsigned", ("app.apk", "app.aab")),
                                     ("support", ("mapping.txt", "native-debug-symbols.zip", "build-info.json"))):
                (root / directory).mkdir()
                for name in names:
                    (root / directory / name).write_bytes(b"untrusted build output")
            def verify():
                return subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True).returncode
            self.assertEqual(verify(), 0)
            artifact = root / "unsigned/app.apk"
            artifact.unlink()
            self.assertNotEqual(verify(), 0)
            artifact.write_bytes(b"apk")
            extra = root / "support/execute.py"
            extra.write_text("raise SystemExit('must never run')")
            self.assertNotEqual(verify(), 0)
            extra.unlink()
            artifact.unlink()
            artifact.symlink_to(root / "unsigned/app.aab")
            self.assertNotEqual(verify(), 0)


if __name__ == "__main__":
    unittest.main()
