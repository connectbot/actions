import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("scan", Path(__file__).parents[1] / "scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

SHA = "a" * 40
REPO = "connectbot/example"
WORKFLOW = ".github/workflows/ci.yml"


class FakeAPI:
    repository = REPO
    def __init__(self):
        self.run = {"id": 42, "repository": {"full_name": REPO}, "path": WORKFLOW,
                    "workflow_id": 7, "run_attempt": 1, "head_sha": SHA, "event": "pull_request",
                    "status": "completed", "conclusion": "success", "pull_requests": [],
                    "head_repository": {"full_name": "contributor/example"}, "head_branch": "feature"}
        self.pr = {"number": 12, "state": "open", "head": {"sha": SHA, "ref": "feature",
                   "repo": {"full_name": "contributor/example"}},
                   "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.artifacts = [{"id": 99, "name": "sonar-inputs-1", "expired": False,
                           "size_in_bytes": 100, "workflow_run": {"id": 42, "head_sha": SHA}}]
        self.refs = SHA
        self.associations = [{"number": 12}]

    def get(self, path):
        if path == "/actions/runs/42":
            return copy.deepcopy(self.run)
        if path.startswith("/pulls/"):
            return copy.deepcopy(self.pr)
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.refs}}
        raise AssertionError(path)

    def pages(self, path, key=None):
        return copy.deepcopy(self.artifacts if key else self.associations)


class DownloadTest(unittest.TestCase):
    def test_auth_only_sent_to_api(self):
        with self.assertRaises(scan.Invalid):
            scan.read_url("https://evil.invalid/data", token="dummy")

    def test_artifact_redirect_drops_auth(self):
        url = "https://api.github.com/repos/connectbot/example/actions/artifacts/99/zip"
        destination = "https://example.blob.core.windows.net/archive?signature=dummy"
        response = io.BytesIO(b"zip bytes")
        error = urllib.error.HTTPError(url, 302, "Found", {"Location": destination}, None)
        with patch.object(scan.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = [error, response]
            self.assertEqual(scan.read_url(url, token="dummy"), b"zip bytes")
            first, second = opener.return_value.open.call_args_list
            self.assertEqual(first.args[0].get_header("Authorization"), "Bearer dummy")
            self.assertIsNone(second.args[0].get_header("Authorization"))

    def test_rejects_other_redirect_hosts(self):
        url = "https://api.github.com/repos/connectbot/example/actions/artifacts/99/zip"
        for destination in ("https://evil.invalid/archive", "http://example.blob.core.windows.net/a",
                            "https://example.blob.core.windows.net.evil.invalid/a",
                            "https://user:pass@example.blob.core.windows.net/a"):
            error = urllib.error.HTTPError(url, 302, "Found", {"Location": destination}, None)
            with patch.object(scan.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.subTest(destination=destination), self.assertRaises(scan.Invalid):
                    scan.read_url(url, token="dummy")

    def test_scanner_checksum_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(scan, "read_url", return_value=b"bad"):
            with self.assertRaises(scan.Invalid):
                scan.install_scanner(Path(temp))
            self.assertFalse((Path(temp) / "scanner").exists())


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.event = {"repository": {"full_name": REPO, "default_branch": "main"},
                      "workflow_run": copy.deepcopy(self.api.run)}

    def resolve(self):
        return scan.resolve(self.api, self.event, WORKFLOW)

    def test_fork_with_empty_associations(self):
        self.assertEqual(self.resolve(), {"sha": SHA, "run_id": 42, "attempt": 1,
                                         "pr": 12, "branch": "feature", "base": "main"})

    def test_same_repository(self):
        self.api.run["pull_requests"] = [{"number": 12}]
        self.api.run["head_repository"]["full_name"] = REPO
        self.api.pr["head"]["repo"]["full_name"] = REPO
        self.assertEqual(self.resolve()["pr"], 12)

    def test_interpolated_branch_rejected(self):
        self.api.run["head_branch"] = "feature${env.SONAR_TOKEN}"
        self.api.pr["head"]["ref"] = "feature${env.SONAR_TOKEN}"
        with self.assertRaises(scan.Invalid):
            self.resolve()

    def test_mismatched_run(self):
        for key, value in (("path", "other.yml"), ("workflow_id", 19), ("head_sha", "b" * 40),
                           ("event", "push"), ("repository", {"full_name": "evil/repo"})):
            with self.subTest(key=key):
                old = self.api.run[key]
                self.api.run[key] = value
                with self.assertRaises(scan.Invalid):
                    self.resolve()
                self.api.run[key] = old

    def test_skips_closed_stale_or_failed(self):
        self.api.pr["state"] = "closed"
        with self.assertRaises(scan.Skip):
            self.resolve()
        self.api.pr["state"] = "open"
        self.api.pr["head"]["sha"] = "b" * 40
        with self.assertRaises(scan.Skip):
            self.resolve()
        self.api.run["conclusion"] = "failure"
        with self.assertRaises(scan.Skip):
            self.resolve()

    def test_attempt_replay(self):
        self.api.run["run_attempt"] = 2
        with self.assertRaises(scan.Skip):
            self.resolve()

    def test_ambiguous_pr(self):
        self.api.associations.append({"number": 13})
        with self.assertRaises(scan.Invalid):
            self.resolve()

    def test_other_head_repository_or_base_rejected(self):
        self.api.pr["head"]["repo"]["full_name"] = "evil/example"
        with self.assertRaises(scan.Skip):
            self.resolve()
        self.api.pr["head"]["repo"]["full_name"] = "contributor/example"
        self.api.pr["base"]["repo"]["full_name"] = "other/example"
        with self.assertRaises(scan.Skip):
            self.resolve()

    def test_branch_baselines(self):
        for event, branch in (("push", "main"), ("push", "release/1.0"), ("schedule", "main")):
            self.api.run.update(event=event, head_branch=branch)
            self.event["workflow_run"] = copy.deepcopy(self.api.run)
            self.assertEqual(self.resolve()["branch"], branch)
        self.api.refs = "b" * 40
        with self.assertRaises(scan.Skip):
            self.resolve()

    def test_unrelated_push_skipped(self):
        self.api.run["event"] = "push"
        self.event["workflow_run"]["event"] = "push"
        with self.assertRaises(scan.Skip):
            self.resolve()

    def test_artifact_attempt_selection(self):
        context = self.resolve()
        self.api.artifacts.append({**self.api.artifacts[0], "name": "sonar-inputs-2"})
        self.assertEqual(scan.select_artifact(self.api, context, "sonar-inputs")["id"], 99)
        self.api.artifacts.append(self.api.artifacts[0])
        with self.assertRaises(scan.Invalid):
            scan.select_artifact(self.api, context, "sonar-inputs")

    def test_bad_artifacts(self):
        for change in ({"expired": True}, {"size_in_bytes": scan.MAX_ARCHIVE + 1},
                       {"name": "wrong"}, {"workflow_run": {"id": 43, "head_sha": SHA}}):
            with self.subTest(change=change):
                old = copy.deepcopy(self.api.artifacts)
                self.api.artifacts[0].update(change)
                with self.assertRaises(scan.Invalid):
                    scan.select_artifact(self.api, self.resolve(), "sonar-inputs")
                self.api.artifacts = old


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, entries):
        path = self.root / "input.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, data in entries:
                z.writestr(name, data)
        return path

    def test_valid_archive(self):
        archive = self.archive([("classes/lib/main/Test.class", b"bytecode"),
                                ("libraries/lib.jar", b"jar"), ("reports/kover.xml", b"<report/>" )])
        scan.unpack(archive, self.root / "inputs")
        self.assertEqual((self.root / "inputs/classes/lib/main/Test.class").read_bytes(), b"bytecode")

    def test_hostile_paths(self):
        for name in ("../escape.class", "/tmp/escape.class", "classes/../escape.class",
                     "classes\\escape.class", "classes/./escape.class", "classes//escape.class",
                     "classes/a.sh", "libraries/a,b.jar", "libraries/${env.SONAR_TOKEN}.jar", "sonar-project.properties", "reports/test\n.xml"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as out:
                    with self.assertRaises(scan.Invalid):
                        scan.unpack(self.archive([(name, b"x")]), Path(out) / "inputs")

    def test_links_rejected(self):
        info = zipfile.ZipInfo("classes/link.class")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(scan.Invalid):
            scan.unpack(self.archive([(info, b"/proc/self/environ")]), self.root / "inputs")

    def test_size_limit(self):
        archive = self.archive([("reports/a.xml", b"1234")])
        with patch.object(scan, "MAX_EXPANDED", 3), self.assertRaises(scan.Invalid):
            scan.unpack(archive, self.root / "inputs")

    def test_duplicate_entries(self):
        with self.assertWarns(UserWarning):
            archive = self.archive([("reports/a.xml", b"x"), ("reports/a.xml", b"y")])
        with self.assertRaises(scan.Invalid):
            scan.unpack(archive, self.root / "inputs")

    def test_report_normalization(self):
        path = self.root / "report.xml"
        path.write_text('<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd"><report/>')
        scan.normalize_report(path)
        self.assertNotIn("DOCTYPE", path.read_text())
        path.write_text('<coverage><sources><source>.github/scripts</source></sources><class filename="a.py"/></coverage>')
        scan.normalize_report(path)

    def test_hostile_reports(self):
        for data in ('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///proc/self/environ">]><report>&x;</report>',
                     '<report><class sourcefilename="../../secret"/></report>',
                     '<coverage><sources><source>/etc</source></sources></coverage>',
                     '<report xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="/etc/passwd"/></report>'):
            path = self.root / "report.xml"
            path.write_text(data)
            with self.subTest(data=data), self.assertRaises(scan.Invalid):
                scan.normalize_report(path)

    def config(self):
        return {"project_key": "example", "organization": "connectbot",
                "properties": {"sonar.sources": "src", "sonar.tests": "test"},
                "artifact_paths": {"sonar.java.binaries": ["classes/main"],
                                   "sonar.coverage.jacoco.xmlReportPaths": ["reports/coverage.xml"]}}

    def test_config_rejects_executable_transport_and_auth_overrides(self):
        for key in ("sonar.host.url", "sonar.token", "sonar.scanner.javaExePath", "sonar.projectBaseDir",
                    "sonar.java.jdkHome", "sonar.scanner.dumpToFile", "project.settings"):
            config = self.config()
            config["properties"][key] = "attacker"
            path = self.root / "config.json"
            path.write_text(json.dumps(config))
            with self.subTest(key=key), self.assertRaises(scan.Invalid):
                scan.load_config(path)

    def test_config_paths_cannot_escape(self):
        config = self.config()
        config["artifact_paths"]["sonar.java.binaries"] = ["classes/../../secret"]
        path = self.root / "config.json"
        path.write_text(json.dumps(config))
        with self.assertRaises(scan.Invalid):
            scan.load_config(path)

    def test_settings_bind_revision_and_ignore_source_settings(self):
        source = self.root / "source"
        inputs = self.root / "inputs"
        for path in (source / "src", source / "test", inputs / "classes/main", inputs / "reports"):
            path.mkdir(parents=True)
        (inputs / "reports/coverage.xml").write_text("<report/>")
        (source / "sonar-project.properties").write_text("sonar.host.url=https://evil.invalid")
        context = {"sha": SHA, "branch": "feature", "base": "main", "pr": 12}
        path = scan.settings(self.config(), context, source, inputs, self.root)
        text = path.read_text()
        self.assertIn("sonar.scm.revision=" + SHA, text)
        self.assertIn("sonar.pullrequest.key=12", text)
        self.assertNotIn("evil.invalid", text)
        self.assertNotIn("sonar.branch.name", text)

    def test_properties_escape_injection(self):
        self.assertEqual(scan.properties_text({"key": "x\\u000asonar.token=bad\nmore"}),
                         "key=x\\\\u000asonar.token=bad\\nmore\n")

    def test_scan_has_minimal_environment_and_safe_logs(self):
        (self.root / "source").mkdir()
        token = "dummy-credential"
        completed = subprocess.CompletedProcess([], 0, token + "\n::error::hostile\n##[error]hostile\n")
        out = io.StringIO()
        with patch.dict(os.environ, {"SONAR_TOKEN": token, "GITHUB_TOKEN": "github-secret",
                                     "JAVA_TOOL_OPTIONS": "-javaagent:evil.jar", "GITHUB_ENV": "evil"}), \
                patch.object(scan.subprocess, "run", return_value=completed) as run, \
                contextlib.redirect_stdout(out):
            self.assertEqual(scan.scan(self.root), 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["SONAR_TOKEN"], token)
        for forbidden in ("GITHUB_TOKEN", "GITHUB_ENV", "JAVA_TOOL_OPTIONS", "GH_TOKEN"):
            self.assertNotIn(forbidden, env)
        self.assertNotIn(token, out.getvalue())
        self.assertNotIn("::error::", out.getvalue())
        self.assertNotIn("##[error]", out.getvalue())
        self.assertIn("-Dproject.settings=", run.call_args.args[0][1])

    def test_source_link_rejected_before_checkout(self):
        context = {"sha": SHA, "branch": "feature", "base": "main", "pr": 12}
        def fake_git(source, *args):
            if args[0] == "rev-parse":
                return SHA.encode() + b"\n"
            if args[0] == "ls-tree":
                return b"120000 blob " + b"b" * 40 + b"\tsrc/link.py\0"
            return b""
        with patch.object(scan, "git", side_effect=fake_git) as run, self.assertRaises(scan.Invalid):
            scan.checkout_source(self.root, REPO, context)
        self.assertFalse(any("checkout" in call.args for call in run.call_args_list))


@unittest.skipUnless(os.environ.get("SONAR_SCANNER_INTEGRATION") == "1", "opt-in scanner download")
class ScannerSmokeTest(unittest.TestCase):
    def test_real_scanner_ignores_pr_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            executable = scan.install_scanner(root)
            (source / "sonar-project.properties").write_text(
                "sonar.host.url=https://invalid.invalid\n"
                "sonar.scanner.javaExePath=/tmp/do-not-execute\n")
            trusted = root / "trusted.properties"
            trusted.write_text("sonar.host.url=https://sonarcloud.io\nsonar.projectKey=example\n")
            dump = root / "dump.properties"
            subprocess.run([str(executable), f"-Dproject.settings={trusted}",
                            f"-Dsonar.scanner.internal.dumpToFile={dump}"],
                           cwd=source, env=scan.clean_environment(root), check=True,
                           capture_output=True, text=True, timeout=60)
            properties = dump.read_text()
            self.assertIn("sonarcloud.io", properties)
            self.assertNotIn("invalid.invalid", properties)
            self.assertNotIn("do-not-execute", properties)


if __name__ == "__main__":
    unittest.main()
