#!/usr/bin/env python3
"""Analyze prebuilt, untrusted JVM/Python projects without executing project code."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

HOST = "https://sonarcloud.io"
SCANNER_VERSION = "8.1.0.6389"
SCANNER_SHA256 = "ab76ab3c360025e9108be5b55be066f304a164f8b2850d2f2f333915db51bc1b"
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_ENTRIES = 50000
SHA = re.compile(r"[0-9a-f]{40}")
# These are analysis settings, never transport, executable, or authentication settings.
PROPERTIES = {
    "sonar.projectName", "sonar.sources", "sonar.tests", "sonar.inclusions",
    "sonar.exclusions", "sonar.test.inclusions", "sonar.test.exclusions",
    "sonar.coverage.exclusions", "sonar.cpd.exclusions", "sonar.sourceEncoding",
    "sonar.java.source", "sonar.kotlin.source.version", "sonar.python.version",
    "sonar.issue.ignore.multicriteria",
}
ARTIFACT_PATHS = {
    "sonar.java.binaries": "classes/",
    "sonar.java.test.binaries": "classes/",
    "sonar.java.libraries": "libraries/",
    "sonar.java.test.libraries": "libraries/",
    "sonar.coverage.jacoco.xmlReportPaths": "reports/",
    "sonar.python.coverage.reportPaths": "reports/",
}


class Invalid(ValueError):
    """An input failed a security or contract check (messages contain no inputs)."""


class Skip(Exception):
    """An obsolete or irrelevant run must not submit an analysis."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def relative(value, glob=False):
    require(isinstance(value, str) and value and len(value) <= 1024, "Invalid path")
    require(not any(ord(c) < 32 for c in value) and "\\" not in value and ":" not in value and "," not in value and "${" not in value,
            "Unsafe path characters")
    p = PurePosixPath(value)
    require(not p.is_absolute() and all(x not in ("", ".", "..") for x in value.split("/")),
            "Path must be relative and normalized")
    require(glob or not any(c in value for c in "*?[]"), "Unexpected path pattern")
    return value


def safe_ref(value):
    require(isinstance(value, str) and value and len(value) < 256
            and not value.startswith(("-", "/")) and not value.endswith(("/", ".", ".lock"))
            and not any(x in value for x in ("..", "@{", "${", "//", "\\"))
            and not re.search(r"[\x00-\x20\x7f~^:?*\[]", value), "Invalid branch")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_url(url, token=None, limit=MAX_ARCHIVE):
    headers = {"User-Agent": "connectbot-sonar", "Accept": "application/vnd.github+json"}
    if token:
        require(url.startswith("https://api.github.com/"), "Invalid authenticated API host")
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.build_opener(NoRedirect).open(
                urllib.request.Request(url, headers=headers), timeout=60) as response:
            data = response.read(limit + 1)
            require(len(data) <= limit, "Download exceeds size limit")
            return data
    except urllib.error.HTTPError as error:
        if error.code == 302 and token and re.fullmatch(
                r"https://api.github.com/repos/[^/]+/[^/]+/actions/artifacts/[0-9]+/zip", url):
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            require(parsed.scheme == "https" and not parsed.username and not parsed.password
                    and parsed.port in (None, 443) and parsed.hostname
                    and parsed.hostname.endswith((".blob.core.windows.net", ".actions.githubusercontent.com")),
                    "Unexpected artifact download host")
            # Signed storage URL: never forward GitHub authorization.
            return read_url(location, limit=limit)
        raise Invalid(f"Download failed with HTTP {error.code}") from None


class GitHub:
    def __init__(self, repository, token):
        require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)),
                "Invalid repository")
        self.repository = repository
        self.token = token

    def get(self, path):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        return json.loads(read_url(f"https://api.github.com/repos/{self.repository}{path}",
                                   self.token, limit=16 * 1024 * 1024))

    def pages(self, path, key=None):
        result = []
        for page in range(1, 101):
            body = self.get(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            rows = body[key] if key else body
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Invalid("API pagination limit exceeded")


def resolve(api, event, workflow):
    """Use server-side run/PR identity; no identity is read from the artifact."""
    trigger = event["workflow_run"]
    require(event["repository"]["full_name"] == api.repository, "Repository mismatch")
    run = api.get(f"/actions/runs/{int(trigger['id'])}")
    require(run["repository"]["full_name"] == api.repository, "Run repository mismatch")
    require(run["path"] == workflow and run["workflow_id"] == trigger["workflow_id"],
            "Unexpected producer workflow")
    if run["run_attempt"] != trigger["run_attempt"]:
        raise Skip("A newer build attempt supersedes this run.")
    require(run["head_sha"] == trigger["head_sha"] and run["event"] == trigger["event"],
            "Run identity mismatch")
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise Skip("The producer did not complete successfully.")
    sha = run["head_sha"]
    require(bool(SHA.fullmatch(sha)), "Invalid run revision")
    context = {"sha": sha, "run_id": run["id"], "attempt": run["run_attempt"]}
    if run["event"] == "pull_request":
        # Fork workflow_run payloads can have an empty pull_requests list.
        numbers = [pr["number"] for pr in run.get("pull_requests", [])]
        if not numbers:
            numbers = [pr["number"] for pr in api.pages(f"/commits/{sha}/pulls")]
        matches = []
        for number in set(numbers):
            pr = api.get(f"/pulls/{int(number)}")
            head_repo = pr["head"].get("repo")
            if (pr["state"] == "open" and pr["base"]["repo"]["full_name"] == api.repository
                    and pr["head"]["sha"] == sha and head_repo
                    and head_repo["full_name"] == run["head_repository"]["full_name"]
                    and pr["head"]["ref"] == run["head_branch"]):
                matches.append(pr)
        if not matches:
            raise Skip("The PR is closed or its head has changed.")
        require(len(matches) == 1, "Ambiguous pull request identity")
        pr = matches[0]
        context.update(pr=pr["number"], branch=safe_ref(pr["head"]["ref"]),
                       base=safe_ref(pr["base"]["ref"]))
    elif run["event"] in ("push", "schedule"):
        branch = safe_ref(run["head_branch"])
        default = event["repository"]["default_branch"]
        if branch != default and (run["event"] != "push" or not branch.startswith("release/")):
            raise Skip("Only default and release branch baselines are analyzed.")
        current = api.get(f"/git/ref/heads/{urllib.parse.quote(branch, safe='')}")
        if current["object"]["sha"] != sha:
            raise Skip("A newer branch head supersedes this run.")
        context.update(branch=branch)
    else:
        raise Skip("Unsupported producer event.")
    return context


def select_artifact(api, context, prefix):
    require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", prefix)), "Invalid artifact prefix")
    name = f"{prefix}-{context['attempt']}"
    artifacts = api.pages(f"/actions/runs/{context['run_id']}/artifacts", "artifacts")
    matches = [item for item in artifacts if item["name"] == name]
    require(len(matches) == 1, "Expected exactly one analysis artifact")
    artifact = matches[0]
    require(not artifact["expired"] and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE,
            "Artifact expired or exceeds size limit")
    if artifact.get("workflow_run"):
        require(artifact["workflow_run"]["id"] == context["run_id"]
                and artifact["workflow_run"]["head_sha"] == context["sha"],
                "Artifact run mismatch")
    return artifact


def unpack(archive, destination):
    """Only plain bytecode, JAR, and report files can cross the artifact boundary."""
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        require(len(bundle.infolist()) <= MAX_ENTRIES, "Too many archive entries")
        seen, size = set(), 0
        for item in bundle.infolist():
            name = relative(item.filename.rstrip("/"))
            require(name not in seen, "Duplicate archive entry")
            seen.add(name)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(kind in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG),
                    "Archive links and special files are forbidden")
            require(not item.flag_bits & 1, "Encrypted archive entry")
            size += item.file_size
            require(item.file_size <= MAX_FILE and size <= MAX_EXPANDED, "Expanded artifact exceeds limit")
            root = name.split("/")[0]
            require(root in ("classes", "libraries", "reports"), "Unexpected artifact directory")
            if item.is_dir():
                continue
            require((root == "classes" and name.endswith(".class"))
                    or (root == "libraries" and name.endswith(".jar"))
                    or (root == "reports" and name.endswith(".xml")), "Unexpected artifact file")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)


def normalize_report(path):
    data = path.read_bytes()
    # Accept the conventional JaCoCo declaration, but never let a scanner resolve it.
    data = data.replace(b'<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">', b'')
    require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper()
            and b"\x00" not in data, "Report DTDs/entities are forbidden")
    tree = ET.fromstring(data)
    require(tree.tag in ("report", "coverage"), "Unsupported coverage report")
    for node in tree.iter():
        require(not node.tag.startswith("{"), "Report XML namespaces are unsupported")
        for key in ("filename", "sourcefilename"):
            if key in node.attrib:
                relative(node.attrib[key])
        if node.tag == "sourcefile" and "name" in node.attrib:
            relative(node.attrib["name"])
        if node.tag == "source" and node.text:
            relative(node.text.strip())
    path.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))


def load_config(path):
    config = json.loads(path.read_text())
    require(set(config) == {"project_key", "organization", "properties", "artifact_paths"},
            "Unknown or missing configuration fields")
    for key in ("project_key", "organization"):
        require(bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", config[key])), "Invalid Sonar project identity")
    for key, value in config["properties"].items():
        require(key in PROPERTIES or re.fullmatch(
            r"sonar\.issue\.ignore\.multicriteria\.[A-Za-z0-9]+\.(ruleKey|resourceKey)", key),
            "Unsupported scanner property")
        require(isinstance(value, str) and not re.search(r"[\x00-\x1f\x7f]", value),
                "Invalid scanner property value")
    require("sonar.sources" in config["properties"], "Source paths are required")
    for key in ("sonar.sources", "sonar.tests"):
        for entry in config["properties"].get(key, "").split(","):
            if entry:
                relative(entry)
    for key, paths in config["artifact_paths"].items():
        require(key in ARTIFACT_PATHS and isinstance(paths, list) and paths, "Invalid artifact mapping")
        for entry in paths:
            relative(entry, glob=True)
            require(entry.startswith(ARTIFACT_PATHS[key]), "Artifact path outside its allowed directory")
    return config


def clean_environment(home):
    # No GitHub command files, tokens, Java agents, proxy settings, or PR PATH entries.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0"}
    if os.environ.get("JAVA_HOME"):
        env["JAVA_HOME"] = os.environ["JAVA_HOME"]
        env["PATH"] = f"{env['JAVA_HOME']}/bin:/usr/bin:/bin"
    return env


def git(source, *args):
    return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=source, env=clean_environment(source.parent),
                          check=True, capture_output=True).stdout


def checkout_source(root, repository, context):
    source = root / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    git(source, "remote", "add", "origin", f"https://github.com/{repository}.git")
    branch = context.get("base", context["branch"])
    refs = [f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
    if "pr" in context:
        refs.append(f"+refs/pull/{context['pr']}/head:refs/remotes/origin/sonar-pr")
    git(source, "fetch", "--no-tags", "origin", *refs)
    expected_ref = "refs/remotes/origin/sonar-pr" if "pr" in context else f"refs/remotes/origin/{branch}"
    if git(source, "rev-parse", expected_ref).decode().strip() != context["sha"]:
        raise Skip("The source ref changed before checkout.")
    # Inspect modes before checkout so Git never creates an attacker-controlled link.
    for entry in git(source, "ls-tree", "-rz", context["sha"]).split(b"\0"):
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        require(metadata.split()[0] in (b"100644", b"100755"), "Source links/submodules are forbidden")
        name = filename.decode("utf-8")
        relative(name)
        require(not any(part in (".scannerwork", ".sonar") for part in name.split("/")),
                "Source contains scanner state")
    git(source, "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false",
        "checkout", "--detach", context["sha"])
    return source


def properties_text(properties):
    # Java properties escaping prevents backslash/newline/Unicode escape injection.
    def escape(value):
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    return "".join(f"{key}={escape(value)}\n" for key, value in sorted(properties.items()))


def settings(config, context, source, inputs, root):
    properties = dict(config["properties"])
    for key in ("sonar.sources", "sonar.tests"):
        if key in properties:
            for name in properties[key].split(","):
                require((source / name).exists(), "Configured source path is missing")
    for key, patterns in config["artifact_paths"].items():
        matches = set()
        for pattern in patterns:
            found = sorted(inputs.glob(pattern))
            require(bool(found), "Configured artifact path is missing")
            matches.update(str(p) for p in found)
        properties[key] = ",".join(sorted(matches))
    properties.update({
        "sonar.host.url": HOST, "sonar.organization": config["organization"],
        "sonar.projectKey": config["project_key"], "sonar.scm.revision": context["sha"],
        "sonar.projectVersion": context["sha"], "sonar.qualitygate.wait": "true",
        "sonar.qualitygate.timeout": "300", "sonar.scanner.skipJreProvisioning": "true",
        "sonar.working.directory": str(root / "scanner-work"),
        "sonar.scanner.metadataFilePath": str(root / "report-task.txt"),
    })
    if "pr" in context:
        properties.update({"sonar.pullrequest.key": str(context["pr"]),
                           "sonar.pullrequest.branch": context["branch"],
                           "sonar.pullrequest.base": context["base"]})
    else:
        properties["sonar.branch.name"] = context["branch"]
    path = root / "trusted.properties"
    path.write_text(properties_text(properties))
    return path


def install_scanner(root):
    data = read_url(f"https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-{SCANNER_VERSION}.zip")
    require(hashlib.sha256(data).hexdigest() == SCANNER_SHA256, "Scanner checksum mismatch")
    archive = root / "scanner.zip"
    archive.write_bytes(data)
    with zipfile.ZipFile(archive) as bundle:
        # This archive is authenticated by the fixed, reviewed checksum above.
        bundle.extractall(root / "scanner")
    executable = root / "scanner" / f"sonar-scanner-{SCANNER_VERSION}" / "bin/sonar-scanner"
    executable.chmod(0o700)
    return executable


def prepare(root, api, event, workflow, prefix, config_path):
    context = resolve(api, event, workflow)
    config = load_config(config_path)
    artifact = select_artifact(api, context, prefix)
    archive = root / "inputs.zip"
    data = read_url(f"https://api.github.com/repos/{api.repository}/actions/artifacts/{int(artifact['id'])}/zip", api.token)
    if artifact.get("digest"):
        require(artifact["digest"] == "sha256:" + hashlib.sha256(data).hexdigest(), "Artifact digest mismatch")
    archive.write_bytes(data)
    inputs = root / "inputs"
    unpack(archive, inputs)
    for report in (inputs / "reports").rglob("*.xml"):
        normalize_report(report)
    source = checkout_source(root, api.repository, context)
    settings(config, context, source, inputs, root)
    install_scanner(root)
    (root / "context.json").write_text(json.dumps(context))
    return context


def scan(root):
    token = os.environ.get("SONAR_TOKEN", "")
    require(bool(token), "SONAR_TOKEN is required")
    home = root / "scanner-home"
    home.mkdir()
    env = clean_environment(home)
    env.update(SONAR_TOKEN=token, SONAR_USER_HOME=str(home / ".sonar"))
    executable = root / "scanner" / f"sonar-scanner-{SCANNER_VERSION}" / "bin/sonar-scanner"
    # Run outside the default project configuration; only the selected trusted file is used.
    # Capture logs: untrusted source text must never become GitHub workflow commands.
    result = subprocess.run([str(executable), f"-Dproject.settings={root / 'trusted.properties'}"],
                            cwd=root / "source", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=1200)
    for line in result.stdout.splitlines():
        print("scanner | " + line.replace(token, "[REDACTED]").replace("::", ": :").replace("##[", "# #["))
    return result.returncode


def summary(message):
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "recheck", "scan"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "scan":
            result = scan(root)
            context = json.loads((root / "context.json").read_text())
            config = load_config(Path(os.environ["SONAR_CONFIG"]))
            query = {"id": config["project_key"]}
            query["pullRequest" if "pr" in context else "branch"] = str(context.get("pr", context["branch"]))
            summary(f"SonarCloud {'completed' if result == 0 else 'failed'}: "
                    f"{HOST}/dashboard?{urllib.parse.urlencode(query)}")
            return result
        require(os.environ["GITHUB_EVENT_NAME"] == "workflow_run", "Expected workflow_run event")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        require(os.environ["GITHUB_REF"] == f"refs/heads/{event['repository']['default_branch']}",
                "Consumer must run from the default branch")
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        workflow = relative(os.environ["SONAR_PRODUCER_WORKFLOW"])
        if args.command == "prepare":
            root.mkdir()
            prepare(root, api, event, workflow, os.environ["SONAR_ARTIFACT_PREFIX"],
                    Path(os.environ["SONAR_CONFIG"]))
        else:
            require(resolve(api, event, workflow) == json.loads((root / "context.json").read_text()),
                    "Analysis context changed")
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("ready=true\n")
        return 0
    except Skip as error:
        summary(str(error))
        return 0
    except Invalid as error:
        summary(f"SonarCloud validation failed: {error}")
        return 1
    except (OSError, KeyError, TypeError, ValueError, ET.ParseError,
            zipfile.BadZipFile, subprocess.SubprocessError):
        # No exception repr/traceback: URLs, process args, and server responses can contain secrets.
        summary("SonarCloud analysis did not complete successfully; retry all producer jobs.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
