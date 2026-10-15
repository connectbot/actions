"""Publication contracts, tamper rejection, uploader behavior and trust boundaries."""

import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
import unittest
import sys
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publication as p


class PublicationTest(unittest.TestCase):
    def setUp(self):
        config = patch.multiple(p, GROUP="org.connectbot.sshlib", MODULES=("protocol", "sshlib"), REPOSITORY="connectbot/cbssh")
        config.start()
        self.addCleanup(config.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "publication"
        self.root.mkdir()
        self.version = "1.2.3-SNAPSHOT"
        self.context = {
            "GITHUB_REPOSITORY": p.REPOSITORY,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": f"{p.REPOSITORY}/.github/workflows/ci.yml@refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_WORKFLOW_SHA": "a" * 40,
            "PUBLICATION_BUILDER_SHA": "b" * 40,
        }
        self.env = patch.dict(os.environ, self.context)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.make_publication()

    def make_publication(self, bundles=False):
        for module in p.MODULES:
            directory = self.root / module
            directory.mkdir(exist_ok=True)
            for path in p.files_for(self.root, module, self.version, bundles):
                path.write_bytes(b"prebuilt artifact\n")
            prefix = directory / f"{module}-{self.version}"
            Path(f"{prefix}.pom").write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                f'<groupId>{p.GROUP}</groupId><artifactId>{module}</artifactId>'
                f'<version>{self.version}</version></project>'
            )
            files = []
            for suffix in (".jar", "-sources.jar", "-javadoc.jar"):
                path = Path(f"{prefix}{suffix}")
                files.append({"name": path.name, "url": path.name, "size": path.stat().st_size,
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            Path(f"{prefix}.module").write_text(json.dumps({
                "component": {"group": p.GROUP, "module": module, "version": self.version},
                "variants": [{"files": files}],
            }))
            Path(f"{prefix}-cyclonedx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}))

    def release_context(self):
        self.version = "1.2.3"
        # Fresh directory because changing a version must never leave old staged files.
        self.root = self.work / "release"
        self.root.mkdir()
        ref = "refs/tags/v1.2.3"
        os.environ.update(GITHUB_REF=ref, GITHUB_WORKFLOW_REF=f"{p.REPOSITORY}/.github/workflows/ci.yml@{ref}")
        self.make_publication(bundles=True)

    def test_complete_publication_and_subject_manifest(self):
        p.prepare(self.root, self.version, self.work / "subjects")
        lines = (self.work / "subjects/sshlib.sha256").read_text().splitlines()
        self.assertEqual(len(lines), 6)
        for line, path in zip(lines, p.files_for(self.root, "sshlib", self.version)):
            digest, name = line.split("  ")
            self.assertEqual(name, path.name)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_supports_single_module_from_another_library(self):
        shutil.rmtree(self.root / "protocol")
        with patch.object(p, "MODULES", ("sshlib",)), patch.object(p, "REPOSITORY", "connectbot/another-library"), \
                patch.dict(os.environ, GITHUB_REPOSITORY="connectbot/another-library",
                           GITHUB_WORKFLOW_REF="connectbot/another-library/.github/workflows/ci.yml@refs/heads/main"):
            p.prepare(self.root, self.version, self.work / "subjects")
        for group in ("../escape", "invalid/group", ""):
            with patch.object(p, "GROUP", group), self.assertRaises(ValueError):
                p.authorize(self.version)
        for modules in (("../escape",), ("protocol", "protocol"), ("",)):
            with patch.object(p, "MODULES", modules), self.assertRaises(ValueError):
                p.authorize(self.version)

    def test_context_records_original_attempt_and_modules(self):
        source = self.work / "source"
        source.mkdir()
        (source / "gradle.properties").write_text(f"version={self.version}\n")
        output = self.work / "output"
        with patch.dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1"):
            p.context(source)
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(fields["artifact-prefix"], "123-1")
        self.assertEqual(json.loads(fields["modules"]), list(p.MODULES))

    def test_matrix_artifact_collection_survives_failed_job_retry(self):
        artifacts = [{"id": index + 1, "name": f"attested-123-1-{module}", "expired": False}
                     for index, module in enumerate(p.MODULES)]
        artifacts.append({"id": 999, "name": "attested-999-1-sshlib", "expired": False})
        output = self.work / "output"
        result = subprocess.CompletedProcess([], 0, json.dumps([{"artifacts": artifacts}]))
        with patch.dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", PUBLICATION_ARTIFACT_PREFIX="123-1"), \
                patch.object(p, "run", return_value=result):
            p.artifact_ids()
            self.assertEqual(output.read_text(), "ids=1,2\n")
            artifacts[0]["expired"] = True
            result.stdout = json.dumps([{"artifacts": artifacts}])
            with self.assertRaises(ValueError):
                p.artifact_ids()

    def test_matrix_job_verifies_only_its_own_module(self):
        self.make_publication(bundles=True)
        shutil.rmtree(self.root / "protocol")
        with patch.object(p, "run", side_effect=self.verified_result) as run:
            p.verify(self.root, self.version, module="sshlib")
        self.assertEqual(run.call_count, 7)
        with self.assertRaises(ValueError):
            p.verify(self.root, self.version, module="../escape")

    def test_publication_authority(self):
        p.authorize(self.version)
        for name, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "schedule"),
                            ("GITHUB_REPOSITORY", "fork/cbssh"), ("GITHUB_REF", "refs/heads/untrusted"),
                            ("GITHUB_WORKFLOW_REF", f"{p.REPOSITORY}/.github/workflows/other.yml@refs/heads/main")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError):
                    p.authorize(self.version)
        self.release_context()
        p.authorize(self.version)
        with self.assertRaises(ValueError):
            p.authorize("1.2.4")

    def test_bad_version_and_path_injection(self):
        for version in ("../../x", "1.2.3\nother=bad", "1.2.3/evil", "1.2.3?query", "1.2.3,evil"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                p.validate(self.root, version)

    def test_reject_extra_missing_and_symlink_files(self):
        extra = self.root / "run.sh"
        extra.write_text("exit 0")
        with self.assertRaises(ValueError):
            p.validate(self.root, self.version)
        extra.unlink()
        jar = p.files_for(self.root, "sshlib", self.version)[0]
        jar.unlink()
        with self.assertRaises(ValueError):
            p.validate(self.root, self.version)
        target = self.work / "outside.jar"
        target.write_bytes(b"prebuilt artifact\n")
        jar.symlink_to(target)
        with self.assertRaises(ValueError):
            p.validate(self.root, self.version)

    def test_reject_tampered_jar_and_coordinates(self):
        jar = p.files_for(self.root, "sshlib", self.version)[0]
        jar.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "metadata"):
            p.validate(self.root, self.version)
        self.make_publication()
        pom = self.root / "protocol" / f"protocol-{self.version}.pom"
        pom.write_text(pom.read_text().replace(p.GROUP, "attacker"))
        with self.assertRaisesRegex(ValueError, "coordinates"):
            p.validate(self.root, self.version)

    def verified_result(self, *args, **kwargs):
        predicate = {"bomFormat": "CycloneDX", "specVersion": "1.6"}
        return subprocess.CompletedProcess(args, 0, json.dumps([{"verificationResult": {"statement": {"predicate": predicate}}}]))

    def test_verify_enforces_certificate_policy_for_every_payload(self):
        self.make_publication(bundles=True)
        with patch.object(p, "run", side_effect=self.verified_result) as run:
            p.verify(self.root, self.version)
        self.assertEqual(run.call_count, 14)
        for call in run.call_args_list:
            args = call.args
            for flag, expected in (("--repo", p.REPOSITORY), ("--signer-workflow", p.WORKFLOW),
                                   ("--signer-digest", "b" * 40), ("--source-digest", "a" * 40),
                                   ("--source-ref", "refs/heads/main")):
                self.assertEqual(args[args.index(flag) + 1], expected)
            self.assertIn("--deny-self-hosted-runners", args)
            self.assertIn("--bundle", args)

    def test_invalid_attestation_prevents_signing_and_upload(self):
        self.make_publication(bundles=True)
        with patch.object(p, "run", side_effect=subprocess.CalledProcessError(1, ["gh"])), \
                patch.object(p, "sign") as sign, patch.object(p, "request") as request:
            with self.assertRaises(subprocess.CalledProcessError):
                p.publish(self.root, self.version)
            sign.assert_not_called()
            request.assert_not_called()

    def test_verified_sbom_must_match_sidecar(self):
        self.make_publication(bundles=True)
        with patch.object(p, "run", return_value=subprocess.CompletedProcess([], 0, '[{"verificationResult":{"statement":{"predicate":{}}}}]')):
            with self.assertRaisesRegex(ValueError, "SBOM"):
                p.verify(self.root, self.version)

    def test_snapshot_deploy_has_one_call_per_module_with_all_attachments(self):
        self.make_publication(bundles=True)
        args = p.snapshot_command(self.root, "sshlib", self.version, self.work / "settings.xml")
        properties = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("-D"))
        files = properties["files"].split(",")
        types = properties["types"].split(",")
        classifiers = properties["classifiers"].split(",")
        self.assertEqual(len(files), len(types))
        self.assertEqual(len(files), len(classifiers))
        self.assertIn(("module", ""), list(zip(types, classifiers)))
        self.assertIn(("json", "provenance"), list(zip(types, classifiers)))
        self.assertIn(("json", "sbom-attestation"), list(zip(types, classifiers)))
        self.assertFalse(any(t.endswith(".asc") for t in types))
        self.assertIn("org.apache.maven.plugins:maven-gpg-plugin:3.2.8:sign-and-deploy-file", args)
        self.assertIn("-Dgpg.bestPractices=true", args)

    def test_snapshot_download_checks_matching_timestamp_and_payload_bytes(self):
        self.make_publication(bundles=True)
        def response(url, **kwargs):
            module = "protocol" if "/protocol/" in url else "sshlib"
            if url.endswith("maven-metadata.xml"):
                metadata = ET.Element("metadata")
                entries = ET.SubElement(ET.SubElement(metadata, "versioning"), "snapshotVersions")
                for suffix in p.PAYLOAD_SUFFIXES + p.BUNDLE_SUFFIXES:
                    classifier, extension = suffix[1:].split(".", 1) if suffix.startswith("-") else ("", suffix[1:])
                    entry = ET.SubElement(entries, "snapshotVersion")
                    ET.SubElement(entry, "classifier").text = classifier
                    ET.SubElement(entry, "extension").text = extension
                    ET.SubElement(entry, "value").text = "1.2.3-20261015.123456-7"
                return ET.tostring(metadata)
            name = url.rsplit("/", 1)[1].replace("1.2.3-20261015.123456-7", self.version)
            return (self.root / module / name).read_bytes()
        with patch.object(p, "run") as run, patch.object(p, "request", side_effect=response):
            p.publish_snapshots(self.root, self.version, self.work)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["cwd"], self.work)
        with patch.object(p, "run"), patch.object(p, "request", side_effect=lambda url, **kw: response(url, **kw) if url.endswith("xml") else b"tampered"):
            with self.assertRaisesRegex(ValueError, "differs"):
                p.publish_snapshots(self.root, self.version, self.work)

    def test_release_bytes_cannot_be_overwritten(self):
        self.release_context()
        def response(url, **kwargs):
            module = "protocol" if "/protocol/" in url else "sshlib"
            return (self.root / module / url.rsplit("/", 1)[1]).read_bytes()
        with patch.object(p, "request", side_effect=response):
            self.assertTrue(p.check_remote(self.root, self.version))
        with patch.object(p, "request", return_value=b"tampered"):
            with self.assertRaisesRegex(ValueError, "conflict"):
                p.check_remote(self.root, self.version)
        with patch.object(p, "request", side_effect=[None, b"prebuilt artifact\n"] + [None] * 14):
            with self.assertRaisesRegex(ValueError, "partially visible"):
                p.check_remote(self.root, self.version, missing=True)

    def test_release_bundle_and_api_failure(self):
        self.release_context()
        for path in p.release_files(self.root, self.version):
            Path(str(path) + ".asc").write_bytes(b"signature")
        with patch.object(p, "check_remote", side_effect=[False, True]), \
                patch.object(p, "run", return_value=subprocess.CompletedProcess([], 0, "12345678-1234-1234-1234-123456789abc")), \
                patch.dict(os.environ, SONATYPE_USERNAME="user", SONATYPE_PASSWORD="secret"), \
                patch.object(p, "request", return_value=b'{"deploymentState":"PUBLISHED"}'):
            p.publish_release(self.root, self.version, self.work)
        with zipfile.ZipFile(self.work / "central-bundle.zip") as archive:
            self.assertEqual(len(archive.namelist()), 64)
            self.assertTrue(all(name.startswith("org/connectbot/sshlib/") for name in archive.namelist()))
            for path in p.release_files(self.root, self.version):
                self.assertEqual(archive.read(f"{p.repository_path(path.parent.name, self.version)}/{path.name}"), path.read_bytes())
                for algorithm in ("md5", "sha1"):
                    checksum = archive.read(f"{p.repository_path(path.parent.name, self.version)}/{path.name}.{algorithm}").decode()
                    self.assertEqual(checksum, hashlib.new(algorithm, path.read_bytes()).hexdigest())
        with patch.object(p, "check_remote", return_value=False), \
                patch.object(p, "run", return_value=subprocess.CompletedProcess([], 0, "12345678-1234-1234-1234-123456789abc")), \
                patch.dict(os.environ, SONATYPE_USERNAME="user", SONATYPE_PASSWORD="secret"), \
                patch.object(p, "request", return_value=b'{"deploymentState":"FAILED"}'):
            with self.assertRaisesRegex(ValueError, "rejected"):
                p.publish_release(self.root, self.version, self.work)

    def test_redirects_are_rejected(self):
        with self.assertRaises(ValueError):
            p.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid")

    @unittest.skipUnless(os.environ.get("PUBLICATION_INTEGRATION") == "1", "opt-in Maven/GPG integration")
    def test_real_snapshot_maven_gpg_upload_and_download(self):
        self.make_publication(bundles=True)
        # A real local HTTP repository exercises Maven's snapshot metadata and
        # detached-signature extension handling without publishing externally.
        repository = self.work / "remote"
        repository.mkdir()

        class Handler(http.server.SimpleHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(repository), **kwargs)

            def do_PUT(self):
                target = Path(self.translate_path(self.path))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        key_source = self.work / "key-source"
        key_source.mkdir(mode=0o700)
        home = self.work / "gnupg"
        self.addCleanup(lambda: subprocess.run(("gpgconf", "--homedir", str(key_source), "--kill", "gpg-agent"), capture_output=True))
        self.addCleanup(lambda: subprocess.run(("gpgconf", "--homedir", str(home), "--kill", "gpg-agent"), capture_output=True))
        p.run("gpg", "--homedir", str(key_source), "--batch", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
              "--quick-generate-key", "Publication integration <publication@example.invalid>", "ed25519", "sign", "0",
              input="disposable-test-passphrase\n", capture_output=True)
        exported = p.run("gpg", "--homedir", str(key_source), "--batch", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
                         "--armor", "--export-secret-keys", input="disposable-test-passphrase\n", capture_output=True)
        with patch.dict(os.environ, MAVEN_GPG_PASSPHRASE="disposable-test-passphrase", MAVEN_GPG_KEYID="", MAVEN_GPG_PRIVATE_KEY=exported.stdout), \
                patch.object(p, "SNAPSHOTS", base):
            p.sign(self.root, self.version, home)
            p.publish_snapshots(self.root, self.version, self.work)
        for module in p.MODULES:
            directory = repository / p.repository_path(module, self.version)
            for path in p.files_for(self.root, module, self.version, bundles=True):
                suffix = path.name.removeprefix(f"{module}-{self.version}")
                candidates = [candidate for candidate in directory.iterdir()
                              if candidate.name.endswith(suffix) and not candidate.name.endswith(".asc")]
                # Main JAR pattern also matches sources and Javadoc: select by contents.
                candidates = [candidate for candidate in candidates if candidate.read_bytes() == path.read_bytes()]
                self.assertTrue(candidates, path.name)
                for candidate in candidates:
                    signature = Path(str(candidate) + ".asc")
                    self.assertTrue(signature.is_file(), signature)
                    p.run("gpg", "--homedir", str(home), "--batch", "--verify", str(signature), str(candidate), capture_output=True)


if __name__ == "__main__":
    unittest.main()
