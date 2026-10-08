#!/usr/bin/env python3
"""Reconcile release documentation, then save a complete GitHub Pages archive.

The archive is a generated Git tree: committing it does not use or change the
source checkout's index. Only this script's --publish path pushes gh-pages.
"""

import argparse
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from docs_site import generate_site
from docs_config import build_fingerprint, load_config, repository_path
from docs_versions import all_tags, discover, git, release_key
from docs_api import generate_reports
from docs_build import assemble_publication


POLICY = 1  # Public-only archive verification contract.


class BuildStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DocumentationConfiguration:
    commit: str
    config: str
    policy: int = POLICY

    @classmethod
    def from_dict(cls, value):
        # Missing policy means the artifact predates explicit visibility filtering.
        return cls(value["commit"], value["config"], value.get("policy", 0))


@dataclass(frozen=True)
class BuildAttempt:
    configuration: DocumentationConfiguration
    status: BuildStatus

    @classmethod
    def from_dict(cls, value):
        configuration = DocumentationConfiguration(
            value.get("commit", ""), value.get("config", ""), value.get("policy", 0))
        return cls(configuration, BuildStatus(value["status"]))

    def to_dict(self):
        return {**asdict(self.configuration),
                "status": self.status.value}


@dataclass
class VersionDocumentation:
    published: DocumentationConfiguration | None = None
    attempt: BuildAttempt | None = None

    @classmethod
    def from_dict(cls, value):
        return cls(
            DocumentationConfiguration.from_dict(value["published"]) if value.get("published") else None,
            BuildAttempt.from_dict(value["attempt"]) if value.get("attempt") else None,
        )

    def to_dict(self):
        result = {}
        if self.published:
            result["published"] = asdict(self.published)
        if self.attempt:
            result["attempt"] = self.attempt.to_dict()
        return result

    def copy_verified_archive(self, source, destination):
        if (self.published and self.published.policy == POLICY
                and (source / "index.html").is_file()
                and (source / "index.md").is_file()):
            shutil.copytree(source, destination)
            return self.published
        return None

    def should_build(self, name, desired, retry_failed, development):
        if (name != development and self.attempt
                and self.attempt.status is BuildStatus.FAILED and not retry_failed):
            return False
        return self.published != desired

    def message(self, name, attempted=False):
        if attempted:
            message = f"{name}: {self.attempt.status.value}"
        elif self.attempt and self.attempt.status is BuildStatus.FAILED:
            message = f"{name}: previous failure; not retried"
        else:
            message = f"{name}: reused"
        if self.attempt and self.attempt.status is BuildStatus.FAILED and self.published:
            message += " (serving previous successful build)"
        return message


@dataclass
class DocumentationManifest:
    versions: dict[str, VersionDocumentation] = field(default_factory=dict)

    @classmethod
    def read(cls, path):
        if not path.exists():
            return cls()
        document = json.loads(path.read_text())
        if document.get("schema") != 1:
            raise RuntimeError("Unknown archive manifest schema")
        return cls({name: VersionDocumentation.from_dict(value)
                    for name, value in document.get("versions", {}).items()})

    def write(self, path):
        document = {"schema": 1,
                    "versions": {name: version.to_dict()
                                 for name, version in self.versions.items()}}
        path.write_text(json.dumps(document, indent=2) + "\n")


def ci_passed(commit, config):
    """Check configured jobs in the latest push run for this exact source commit."""
    rules = config.get("ci")
    if not rules:
        return True
    slug = config["repository"]["slug"]
    workflow = f"/repos/{slug}/actions/workflows/{rules['workflow']}/runs"
    runs = json.loads(subprocess.check_output([
        "gh", "api", "--method", "GET", workflow,
        "-f", f"head_sha={commit}", "-f", f"event={rules['event']}", "-f", "per_page=1",
    ]))["workflow_runs"]
    if not runs or runs[0]["head_sha"] != commit:
        return False
    pages = json.loads(subprocess.check_output([
        "gh", "api", "--paginate", "--slurp",
        f"/repos/{slug}/actions/runs/{runs[0]['id']}/jobs?per_page=100",
    ]))
    jobs = [job for page in pages for job in page["jobs"]]
    for rule in rules["jobs"]:
        matches = [job for job in jobs if job["name"] == rule["name"]]
        if len(matches) != 1:
            return False
        job = matches[0]
        if job["conclusion"] == "success":
            continue
        fallback = rule.get("fallback_steps")
        if not fallback or job["conclusion"] is None:
            return False
        steps = {step["name"]: step["conclusion"] for step in job.get("steps", [])}
        for step in fallback:
            if step["name"] not in steps and step.get("optional"):
                continue
            if steps.get(step["name"]) not in step["conclusions"]:
                return False
    return True


def load_archive(repo, archive, settings):
    result = subprocess.run(["git", "-C", str(repo), "ls-remote", "--exit-code",
                             settings["archive"]["remote"], f"refs/heads/{settings['archive']['branch']}"], capture_output=True)
    if result.returncode == 2:
        return None, DocumentationManifest()
    if result.returncode:
        raise RuntimeError(result.stderr.decode())
    git(repo, "fetch", "--no-tags", settings["archive"]["remote"], f"refs/heads/{settings['archive']['branch']}")
    parent = git(repo, "rev-parse", "FETCH_HEAD").decode().strip()
    with tempfile.TemporaryFile() as packed:
        subprocess.run(["git", "-C", str(repo), "archive", parent], stdout=packed, check=True)
        packed.seek(0)
        with tarfile.open(fileobj=packed) as contents:
            contents.extractall(archive, filter="data")
    return parent, DocumentationManifest.read(archive / "manifest.json")


def build_version(repo, settings, commit, name, destination, log):
    with tempfile.TemporaryDirectory(prefix="docs-build-") as temporary:
        checkout = Path(temporary) / "source"
        with log.open("w", encoding="utf-8") as output:
            subprocess.run(["git", "clone", "--shared", "--no-checkout", str(repo), str(checkout)],
                           stdout=output, stderr=subprocess.STDOUT, check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", commit],
                           stdout=output, stderr=subprocess.STDOUT, check=True)
            # Give adapters the validated current config, even for historical sources.
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps(settings), encoding="utf-8")
            values = {"repo": str(repo), "checkout": str(checkout), "output": str(destination),
                      "commit": commit, "version": name, "config": str(config_path),
                      "publisher": str(Path(__file__).resolve().parent),
                      "repository_url": settings["repository"]["url"]}
            command = [arg.format_map(values) for arg in settings["build"]["command"]]
            subprocess.run(command, cwd=checkout, stdout=output, stderr=subprocess.STDOUT,
                           check=True, timeout=settings["build"]["timeout"])
        if not (destination / "index.html").is_file() or not (destination / "index.md").is_file():
            raise RuntimeError("Build adapter did not produce HTML and Markdown entrypoints")


def reconcile(repo, settings, archive, output, logs, previous, versions, config,
              retry_failed=False, builder=build_version, admission=None):
    records = {}
    messages = []
    development = settings["repository"]["development_ref"]
    for name, commit in versions.items():
        old = previous.versions.get(name, VersionDocumentation())
        record = VersionDocumentation(attempt=old.attempt)
        record.published = old.copy_verified_archive(archive / name, output / name)
        desired = DocumentationConfiguration(commit, config)
        files = git(repo, "ls-tree", "--name-only", commit, settings["build"]["probe"]["path"]).decode()
        build_file = git(repo, "show", f"{commit}:{settings['build']['probe']['path']}").decode() if files.strip() else ""
        if admission is not None and not admission(commit):
            messages.append(f"{name}: pending (configured CI checks have not succeeded)")
        elif not re.search(settings["build"]["probe"]["pattern"], build_file):
            record.attempt = BuildAttempt(desired, BuildStatus.SKIPPED)
            messages.append(f"{name}: skipped (no Dokka configuration)")
        elif record.should_build(name, desired, retry_failed, development):
            print(f"Building documentation for {name} ({commit})", flush=True)
            status = BuildStatus.FAILED
            # Build separately so failures cannot damage the previous successful tree.
            with tempfile.TemporaryDirectory(prefix="docs-result-") as temporary:
                result = Path(temporary) / "result"
                log = logs / f"{name}.log"
                try:
                    builder(repo, settings, commit, name, result, log)
                    if any(path.is_symlink() for path in result.rglob("*")):
                        raise RuntimeError("Dokka output contains symlinks")
                except (subprocess.SubprocessError, OSError, RuntimeError) as error:
                    with log.open("a") as failed_log:
                        failed_log.write(f"\nPublisher: {error}\n")
                else:
                    # Assembly errors are fatal: never publish a partially copied tree.
                    if (output / name).exists():
                        shutil.rmtree(output / name)
                    shutil.copytree(result, output / name)
                    record.published = desired
                    status = BuildStatus.SUCCESS
                record.attempt = BuildAttempt(desired, status)
            messages.append(record.message(name, attempted=True))
        else:
            messages.append(record.message(name))
        records[name] = record
    return DocumentationManifest(records), messages


def write_tree(repo, directory):
    """Store generated files without touching the developer's Git index."""
    entries = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise RuntimeError(f"Archive cannot contain symlinks: {path}")
        if path.is_dir():
            mode, kind, oid = "040000", "tree", write_tree(repo, path)
        else:
            mode, kind = "100644", "blob"
            oid = git(repo, "hash-object", "-w", str(path)).decode().strip()
        entries.append(f"{mode} {kind} {oid}\t{path.name}\0".encode())
    return git(repo, "mktree", "-z", data=b"".join(entries)).decode().strip()


def save_archive(repo, output, parent, settings):
    tree = write_tree(repo, output)
    if parent and git(repo, "rev-parse", f"{parent}^{{tree}}").decode().strip() == tree:
        return
    args = ["-c", f"user.name={settings['archive']['author_name']}", "-c",
            f"user.email={settings['archive']['author_email']}",
            "commit-tree", tree]
    if parent:
        args.extend(["-p", parent])
    commit = git(repo, *args, data=b"Update versioned public API documentation\n").decode().strip()
    # An ordinary fast-forward push detects external writers instead of overwriting them.
    git(repo, "push", settings["archive"]["remote"], f"{commit}:refs/heads/{settings['archive']['branch']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--retry-failed", choices=["true", "false"], default="false")
    parser.add_argument("--require-ci", action="store_true", help="Require configured CI admission")
    parser.add_argument("--publish", action="store_true", help="Push the generated archive")
    args = parser.parse_args()
    repo = args.repo.resolve()
    config_path = args.config if args.config.is_absolute() else repository_path(repo, str(args.config))
    try:
        settings = load_config(repo, config_path)
    except (ValueError, KeyError, TypeError, OSError, re.error) as error:
        parser.error(str(error))
    require_ci = args.require_ci or (args.publish and settings.get("ci", {}).get("required_for_publish", False))
    if require_ci and not settings.get("ci"):
        parser.error("CI admission requested but no ci configuration exists")
    output = args.output.resolve()
    if output.is_relative_to(repo) or repo.is_relative_to(output):
        parser.error("--output must be outside the source repository")
    output.mkdir(parents=True, exist_ok=False)
    args.logs.mkdir(parents=True, exist_ok=True)
    fingerprint = build_fingerprint(repo, settings)
    versions = discover(repo, settings)
    with tempfile.TemporaryDirectory(prefix="docs-archive-") as temporary:
        archive = Path(temporary)
        parent, previous = load_archive(repo, archive, settings)
        manifest, messages = reconcile(repo, settings, archive, output, args.logs, previous,
                                      versions, fingerprint, args.retry_failed == "true",
                                      admission=(lambda commit: ci_passed(commit, settings)) if require_ci else None)
    reports = generate_reports(repo, settings, manifest, all_tags(repo), output)
    manifest.write(output / "manifest.json")
    (output / ".nojekyll").touch()
    ready = generate_site(output, versions, settings, reports)
    failures = sum(record.published is None or
                   (record.attempt is not None and record.attempt.status is BuildStatus.FAILED) or
                   record.published != DocumentationConfiguration(versions[name], fingerprint)
                   for name, record in manifest.versions.items())
    summary = "## Documentation versions\n\n" + "\n".join(f"- {message}" for message in messages) + "\n"
    if not ready:
        summary += "\nNo verified development documentation is available; the live site will be left unchanged.\n"
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary)
    if args.publish:
        save_archive(repo, output, parent, settings)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"ready={str(ready).lower()}\nfailures={failures}\n")


if __name__ == "__main__":
    main()
