#!/usr/bin/env python3
# Copyright 2026 Kenny Root
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_publishing as fixtures
p = fixtures.p


identity = fixtures.identity

class PolicyTests(unittest.TestCase):
    setUp = fixtures.HandoffTests.setUp
    populate = fixtures.HandoffTests.populate
    refresh = fixtures.HandoffTests.refresh
    def responses(self, policy=None):
        data = p.json_bytes(policy or self.policy)
        return [{"id": p.POLICY_REPOSITORY_ID}, {"protected": True, "commit": {"sha": "a" * 40}},
            {"path": ".github/android-signer/42.json", "type": "file", "encoding": "base64", "content": base64.b64encode(data).decode()}]

    def fetch(self, responses):
        out = self.root / "loaded-policy.json"
        with mock.patch.dict(os.environ, {**self.env, "GITHUB_REPOSITORY_ID": "42"}, clear=True), \
             mock.patch.object(p, "policy_api", side_effect=responses) as api:
            p.fetch_policy(out)
        return out, api

    def test_central_policy_uses_platform_repository_id_and_immutable_commit(self):
        out, api = self.fetch(self.responses())
        self.assertEqual(p.load(out.read_bytes()), self.policy)
        self.assertEqual(api.call_args.args, ("/contents/.github/android-signer/42.json?ref=" + "a" * 40,))
        source = p.load(out.with_suffix(".source.json").read_bytes())
        self.assertEqual(source["commit"], "a" * 40)
        self.assertEqual(source["sha256"], p.digest(out.read_bytes()))

    def test_changed_policy_repository_or_unprotected_branch_is_rejected(self):
        for index, replacement in [(0, {"id": 99}), (1, {"protected": False, "commit": {"sha": "a" * 40}})]:
            responses = self.responses()
            responses[index] = replacement
            with self.assertRaises(ValueError):
                self.fetch(responses)
            self.assertFalse((self.root / "loaded-policy.json").exists())

    def test_another_source_repository_policy_is_rejected(self):
        policy = {**self.policy, "repository_id": 43}
        with self.assertRaisesRegex(ValueError, "another repository"):
            self.fetch(self.responses(policy))

    def test_trusted_builder_identity_requires_repository_id_and_branch_protection(self):
        for responses in ([{"id": 99, "visibility": "public"}],
                          [{"id": 1369420724, "visibility": "public"}, {"protected": False}]):
            with mock.patch.object(p.r, "api", side_effect=responses), self.assertRaises(ValueError):
                p.r.verify_workflow_identity(identity())

    def test_removed_authorization_modes_and_missing_finalizer_are_rejected(self):
        for field, value in (("authorization", "content-pins"), ("authorization", "workflow-identity"),
                             ("sha256", []), ("helpers", {})):
            policy = {**self.policy, "builder": {**self.policy["builder"], field: value}}
            with self.assertRaisesRegex(ValueError, "unsupported workflow policy"):
                p.r.validate_policy(policy)
        old = {**self.policy, "version": 2}
        with self.assertRaisesRegex(ValueError, "version 3"):
            p.r.validate_policy(old)
        missing = {k: v for k, v in self.policy.items() if k != "finalizer"}
        with self.assertRaisesRegex(ValueError, "required policy identities"):
            p.r.validate_policy(missing)
        with self.assertRaises(ValueError):
            p.r.validate_policy({**self.policy, "callers": {".github/workflows/ci.yml": []}})

    def test_shared_finalizer_certificate_and_predicate_must_agree(self):
        trusted = identity("android-finalize.yml")
        self.policy["finalizer"] = trusted
        self.policy_path.write_bytes(p.json_bytes(self.policy))
        signer = "https://github.com/connectbot/actions/.github/workflows/android-finalize.yml@refs/heads/android-signer-v1"
        certificate = self.result["verificationResult"]["signature"]["certificate"]
        certificate.update(buildSignerURI=signer, buildSignerDigest="1" * 40)
        self.predicate.update(runDetails={"builder": {"id": signer}})
        self.predicate["buildDefinition"]["internalParameters"] = {"finalizerCommit": "1" * 40}
        self.populate()
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(p.r, "api", return_value={"id": 42, "visibility": "public", "default_branch": "main"}), \
             mock.patch.object(p.r, "verify_workflow_identity"), \
             mock.patch.object(p.r, "run", return_value=p.json_bytes([self.result])) as run:
            self.assertEqual(p.verify_handoff(self.handoff, self.sha, self.policy_path), self.manifest)
            self.assertNotIn("--signer-digest", run.call_args.args)
            certificate["buildSignerURI"] = signer.replace("android-finalize.yml", "attacker.yml")
            run.return_value = p.json_bytes([self.result])
            with self.assertRaisesRegex(ValueError, "builder differs"):
                p.verify_handoff(self.handoff, self.sha, self.policy_path)


if __name__ == "__main__":
    unittest.main()
