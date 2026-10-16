#!/usr/bin/env python3
# Copyright 2026 Kenny Root
from pathlib import Path
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

    def test_fixed_build_separates_gradle_from_attestation_credentials(self):
        workflow = (ROOT / ".github/workflows/android-build.yml").read_text()
        build, attest = workflow.split("  attest-unsigned:\n")
        self.assertNotIn("    inputs:", build)
        self.assertNotIn("id-token: write", build)
        self.assertNotIn("attestations: write", build)
        self.assertNotIn("actions/checkout@", attest)
        self.assertNotIn("gradlew", attest)
        self.assertIn("support/build-info.json", attest)
        self.assertIn("name: unsigned-${{ matrix.flavor }}-${{ github.run_attempt }}", build)
        self.assertIn("name: release-support-${{ matrix.flavor }}-${{ github.run_attempt }}", build)


if __name__ == "__main__":
    unittest.main()
