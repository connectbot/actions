#!/usr/bin/env python3
"""Validate, verify and publish prebuilt Maven files; never execute staged code."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


GROUP = os.environ.get("PUBLICATION_GROUP", "")
MODULES = tuple(os.environ.get("PUBLICATION_MODULES", "").split(","))
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
CALLER_WORKFLOW = os.environ.get("PUBLICATION_CALLER_WORKFLOW", ".github/workflows/ci.yml")
TAG_PREFIX = os.environ.get("PUBLICATION_TAG_PREFIX", "v")
WORKFLOW = "connectbot/actions/.github/workflows/publish-maven.yml"
CENTRAL = "https://central.sonatype.com"
MAVEN = "https://repo.maven.apache.org/maven2"
SNAPSHOTS = f"{CENTRAL}/repository/maven-snapshots"
PROVENANCE = "https://slsa.dev/provenance/v1"
CYCLONEDX = "https://cyclonedx.org/bom"
PAYLOAD_SUFFIXES = (".jar", "-sources.jar", "-javadoc.jar", ".pom", ".module", "-cyclonedx.json")
BUNDLE_SUFFIXES = ("-provenance.json", "-sbom-attestation.json")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def version_check(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9]+)*", version):
        raise ValueError(f"Invalid publication version: {version!r}")


def authorize(version):
    """Use GitHub context, never caller-supplied inputs, for publication authority."""
    version_check(version)
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", GROUP):
        raise ValueError("Invalid Maven group")
    if not MODULES or len(set(MODULES)) != len(MODULES) or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", m) for m in MODULES):
        raise ValueError("Expected a comma-separated list of unique Maven module names")
    if not re.fullmatch(r"[A-Za-z0-9._-]*", TAG_PREFIX):
        raise ValueError("Invalid tag prefix")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPOSITORY):
        raise ValueError("Invalid source repository")
    ref = os.environ["GITHUB_REF"]
    if (os.environ["GITHUB_REPOSITORY"] != REPOSITORY
            or os.environ["GITHUB_EVENT_NAME"] != "push"
            or os.environ["GITHUB_WORKFLOW_REF"] != f"{REPOSITORY}/{CALLER_WORKFLOW}@{ref}"):
        raise ValueError("Publication requires the configured source push workflow")
    if version.endswith("-SNAPSHOT"):
        if ref != "refs/heads/main" and not re.fullmatch(r"refs/heads/release/[0-9]+\.[0-9]+", ref):
            raise ValueError("Snapshots require main or a numeric release branch")
    elif ref != f"refs/tags/{TAG_PREFIX}{version}":
        raise ValueError("Release tag must match the project version")
    for name in ("GITHUB_SHA", "PUBLICATION_BUILDER_SHA"):
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ[name]):
            raise ValueError(f"Invalid {name}")


def files_for(root, module, version, bundles=False):
    suffixes = PAYLOAD_SUFFIXES + (BUNDLE_SUFFIXES if bundles else ())
    return [root / module / f"{module}-{version}{suffix}" for suffix in suffixes]


def validate(root, version, bundles=False, modules=None):
    version_check(version)
    expected = set()
    for module in MODULES if modules is None else modules:
        expected.add(root / module)
        files = files_for(root, module, version, bundles)
        expected.update(files)
        for path in files:
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Missing, empty or unsafe publication file: {path}")
        pom = ET.parse(root / module / f"{module}-{version}.pom").getroot()
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        if [pom.findtext(f"m:{key}", namespaces=ns) for key in ("groupId", "artifactId", "version")] != [GROUP, module, version]:
            raise ValueError(f"Unexpected POM coordinates for {module}")
        metadata = json.loads((root / module / f"{module}-{version}.module").read_text())
        component = metadata["component"]
        if [component[key] for key in ("group", "module", "version")] != [GROUP, module, version]:
            raise ValueError(f"Unexpected Gradle coordinates for {module}")
        for variant in metadata["variants"]:
            for entry in variant.get("files", []):
                path = root / module / entry["url"]
                if path not in files or entry["name"] != path.name:
                    raise ValueError("Gradle metadata references an unexpected file")
                data = path.read_bytes()
                if entry["size"] != len(data):
                    raise ValueError("Gradle metadata size mismatch")
                for algorithm in ("sha256", "sha512", "sha1", "md5"):
                    if algorithm in entry and hashlib.new(algorithm, data).hexdigest() != entry[algorithm]:
                        raise ValueError("Gradle metadata digest mismatch")
        sbom = json.loads((root / module / f"{module}-{version}-cyclonedx.json").read_text())
        if sbom.get("bomFormat") != "CycloneDX":
            raise ValueError("Expected a CycloneDX SBOM")
    actual = set(root.rglob("*"))
    if actual != expected or any(path.is_symlink() for path in actual):
        raise ValueError(f"Unexpected publication paths: {actual - expected}")


def prepare(root, version, destination):
    authorize(version)
    validate(root, version)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for module in MODULES:
        checksums = destination / f"{module}.sha256"
        checksums.write_text("".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in files_for(root, module, version)
        ))
        outputs[f"{module}_jar"] = str(root / module / f"{module}-{version}.jar")
        outputs[f"{module}_sbom"] = str(root / module / f"{module}-{version}-cyclonedx.json")
    # The matrix job selects from the validated module list, never from arbitrary paths.
    selected = os.environ.get("PUBLICATION_MATRIX_MODULE")
    if selected is not None:
        if selected not in MODULES:
            raise ValueError("Unexpected matrix module")
        outputs["jar"] = outputs[f"{selected}_jar"]
        outputs["sbom"] = outputs[f"{selected}_sbom"]
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in outputs.items())


def verify(root, version, module=None):
    authorize(version)
    if module is not None and module not in MODULES:
        raise ValueError("Unexpected module")
    modules = MODULES if module is None else (module,)
    validate(root, version, bundles=True, modules=modules)
    policy = (
        "--repo", REPOSITORY, "--signer-workflow", WORKFLOW,
        "--signer-digest", os.environ["PUBLICATION_BUILDER_SHA"],
        "--source-digest", os.environ["GITHUB_SHA"],
        "--source-ref", os.environ["GITHUB_REF"], "--deny-self-hosted-runners",
    )
    for module in modules:
        prefix = root / module / f"{module}-{version}"
        for path in files_for(root, module, version):
            run("gh", "attestation", "verify", str(path), "--bundle", f"{prefix}-provenance.json",
                "--predicate-type", PROVENANCE, *policy, capture_output=True)
        result = run(
            "gh", "attestation", "verify", f"{prefix}.jar", "--bundle", f"{prefix}-sbom-attestation.json",
            "--predicate-type", CYCLONEDX, "--format", "json", *policy, capture_output=True,
        )
        expected = json.loads(Path(f"{prefix}-cyclonedx.json").read_text())
        if not any(item["verificationResult"]["statement"]["predicate"] == expected
                   for item in json.loads(result.stdout)):
            raise ValueError("Verified SBOM does not match the published SBOM")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing an HTTP redirect during publication")


def request(url, *, data=None, headers=None, method=None, missing=False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        # Do not print response bodies/requests that might contain credentials.
        raise RuntimeError(f"Publication HTTP request failed: {error.code}") from None


def sign(root, version, home):
    home.mkdir(mode=0o700)
    run("gpg", "--homedir", str(home), "--batch", "--import",
        input=os.environ["MAVEN_GPG_PRIVATE_KEY"], capture_output=True)
    result = run("gpg", "--homedir", str(home), "--batch", "--with-colons", "--list-secret-keys", capture_output=True)
    fingerprints = [line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:")]
    key = os.environ.get("MAVEN_GPG_KEYID") or fingerprints[0]
    for module in MODULES:
        for path in files_for(root, module, version, bundles=True):
            run("gpg", "--homedir", str(home), "--batch", "--yes", "--armor", "--detach-sign",
                "--local-user", key, "--pinentry-mode", "loopback", "--passphrase-fd", "0", str(path),
                input=os.environ.get("MAVEN_GPG_PASSPHRASE", "") + "\n", capture_output=True)


def repository_path(module, version):
    return f"{GROUP.replace('.', '/')}/{module}/{version}"


def release_files(root, version):
    return [path for module in MODULES for path in files_for(root, module, version, bundles=True)]


def check_remote(root, version, *, missing=False):
    """Compare every attested file and bundle, not just the main JAR."""
    found = []
    for module in MODULES:
        for path in files_for(root, module, version, bundles=True):
            data = request(f"{MAVEN}/{repository_path(module, version)}/{path.name}", missing=missing)
            found.append(data is not None)
            if data is not None and data != path.read_bytes():
                raise ValueError(f"Published bytes conflict with {path.name}; never overwrite a release")
    if any(found) and not all(found):
        raise ValueError("Release is partially visible; retry the publisher after Central finishes propagating")
    return all(found)


def publish_release(root, version, work):
    if check_remote(root, version, missing=True):
        # GPG signatures contain a signing time. Reuse the published signatures
        # on retries so GitHub assets remain identical to the Maven publication.
        for module in MODULES:
            for path in files_for(root, module, version, bundles=True):
                signature = Path(str(path) + ".asc")
                signature.write_bytes(request(f"{MAVEN}/{repository_path(module, version)}/{signature.name}"))
                run("gpg", "--homedir", str(work / "gnupg"), "--batch", "--verify",
                    str(signature), str(path), capture_output=True)
        return
    archive = work / "central-bundle.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for module in MODULES:
            for path in sorted((root / module).iterdir()):
                bundle.write(path, f"{repository_path(module, version)}/{path.name}")
                if not path.name.endswith(".asc"):
                    for algorithm in ("md5", "sha1"):
                        digest = hashlib.new(algorithm, path.read_bytes()).hexdigest()
                        bundle.writestr(f"{repository_path(module, version)}/{path.name}.{algorithm}", digest)
    token = base64.b64encode(
        f"{os.environ['SONATYPE_USERNAME']}:{os.environ['SONATYPE_PASSWORD']}".encode()
    ).decode()
    headers = {"Authorization": f"Bearer {token}"}
    # curl reads credentials from stdin rather than exposing them in the process arguments.
    result = run(
        "curl", "--fail", "--silent", "--show-error", "--proto", "=https", "--config", "-",
        "--form", f"bundle=@{archive}",
        f"{CENTRAL}/api/v1/publisher/upload?publishingType=AUTOMATIC&name=cbssh-{version}",
        input=f'header = "Authorization: Bearer {token}"\n', capture_output=True,
    )
    deployment = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", deployment):
        raise ValueError("Unexpected Central deployment ID")
    (work / "deployment-id.txt").write_text(deployment + "\n")
    summary(f"Central deployment: `{deployment}`")
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        status = json.loads(request(f"{CENTRAL}/api/v1/publisher/status?id={deployment}",
                                    method="POST", data=b"", headers=headers))
        state = status["deploymentState"]
        if state == "PUBLISHED":
            break
        if state == "FAILED":
            raise ValueError(f"Central rejected deployment {deployment}; inspect its validation errors")
        time.sleep(15)
    else:
        raise TimeoutError(f"Central deployment {deployment} did not reach PUBLISHED")
    # The Central CDN may lag the deployment status.
    for attempt in range(40):
        try:
            if check_remote(root, version, missing=True):
                return
        except ValueError as error:
            if "partially visible" not in str(error):
                raise
        time.sleep(15)
    raise TimeoutError("Published release is not yet completely visible on Maven Central")


def snapshot_command(root, module, version, settings):
    prefix = f"{module}-{version}"
    extras = []
    classifiers = []
    types = []
    for suffix in PAYLOAD_SUFFIXES + BUNDLE_SUFFIXES:
        if suffix not in (".jar", ".pom"):
            classifier, extension = suffix[1:].split(".", 1) if suffix.startswith("-") else ("", suffix[1:])
            extras.append(str(root / module / f"{prefix}{suffix}"))
            classifiers.append(classifier)
            types.append(extension)
    # The GPG goal generates signatures with the correct multi-part extensions
    # and deploys everything together. deploy:deploy-file collapses .jar.asc,
    # .pom.asc and .module.asc into conflicting artifacts with extension .asc.
    return (
        "mvn", "--batch-mode", "--no-transfer-progress", "--settings", str(settings),
        "org.apache.maven.plugins:maven-gpg-plugin:3.2.8:sign-and-deploy-file",
        f"-Dgpg.homedir={settings.parent / 'gnupg'}", "-Dgpg.bestPractices=true",
        "-Dgpg.passphraseEnvName=MAVEN_GPG_PASSPHRASE",
        *([f"-Dgpg.keyname={os.environ['MAVEN_GPG_KEYID']}"] if os.environ.get("MAVEN_GPG_KEYID") else []),
        f"-Durl={SNAPSHOTS}", "-DrepositoryId=central-snapshots",
        f"-Dfile={root / module / (prefix + '.jar')}",
        f"-DpomFile={root / module / (prefix + '.pom')}", "-DgeneratePom=false",
        "-Dfiles=" + ",".join(extras), "-Dclassifiers=" + ",".join(classifiers),
        "-Dtypes=" + ",".join(types),
    )


def publish_snapshots(root, version, work):
    # An empty working directory prevents Maven from loading source-controlled .mvn extensions/POMs.
    settings = work / "settings.xml"
    settings.write_text("""<settings><servers><server><id>central-snapshots</id>
<username>${env.SONATYPE_USERNAME}</username><password>${env.SONATYPE_PASSWORD}</password>
</server></servers></settings>""")
    settings.chmod(0o600)
    for module in MODULES:
        run(*snapshot_command(root, module, version, settings), cwd=work)
        remote = f"{SNAPSHOTS}/{repository_path(module, version)}"
        metadata = ET.fromstring(request(f"{remote}/maven-metadata.xml"))
        resolved_versions = set()
        for path in files_for(root, module, version, bundles=True):
            suffix = path.name.removeprefix(f"{module}-{version}")
            classifier, extension = suffix[1:].split(".", 1) if suffix.startswith("-") else ("", suffix[1:])
            matches = [entry.findtext("value") for entry in metadata.findall("./versioning/snapshotVersions/snapshotVersion")
                       if (entry.findtext("classifier") or "") == classifier and entry.findtext("extension") == extension]
            if len(matches) != 1:
                raise ValueError(f"Snapshot metadata does not identify {path.name}")
            resolved_versions.add(matches[0])
            filename = f"{module}-{matches[0]}{suffix}"
            if request(f"{remote}/{filename}") != path.read_bytes():
                raise ValueError(f"Snapshot download differs from {path.name}")
        if len(resolved_versions) != 1:
            raise ValueError("Snapshot attachments do not share one timestamp and build number")
        summary(f"Snapshot verified: `{module}:{version}` at `{remote}`")


def summary(message):
    print(message)
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n\n")


def context(source):
    versions = [line.removeprefix("version=") for line in (source / "gradle.properties").read_text().splitlines()
                if line.startswith("version=")]
    if len(versions) != 1:
        raise ValueError("Expected exactly one project version")
    version = versions[0]
    version_check(version)
    selected = os.environ["GITHUB_REF"].startswith("refs/tags/") or version.endswith("-SNAPSHOT")
    if selected:
        authorize(version)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"version={version}\npublish={str(selected).lower()}\n")
        output.write(f"modules={json.dumps(MODULES)}\n")
        output.write(f"artifact-prefix={os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}\n")


def artifact_ids():
    """Resolve this run's expected matrix artifacts, including failed-job retries."""
    prefix = os.environ["PUBLICATION_ARTIFACT_PREFIX"]
    if not re.fullmatch(r"[0-9]+-[0-9]+", prefix):
        raise ValueError("Invalid artifact prefix")
    result = run("gh", "api", "--paginate", "--slurp",
                 f"repos/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}/artifacts", capture_output=True)
    artifacts = [item for page in json.loads(result.stdout) for item in page["artifacts"]]
    ids = []
    for module in MODULES:
        expected = f"attested-{prefix}-{module}"
        matches = [a for a in artifacts if a["name"] == expected and not a["expired"]]
        if len(matches) != 1 or not isinstance(matches[0]["id"], int):
            raise ValueError(f"Expected exactly one immutable artifact: {expected}")
        ids.append(str(matches[0]["id"]))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("ids=" + ",".join(ids) + "\n")


def github_release(root, version, work):
    assets = [path for module in MODULES for path in sorted((root / module).iterdir())]
    manifest = work / "SHA256SUMS"
    manifest.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets))
    assets.append(manifest)
    tag = f"{TAG_PREFIX}{version}"
    # Upload to a draft first so a partially populated release is not advertised.
    result = subprocess.run(("gh", "release", "view", tag, "--repo", REPOSITORY, "--json", "isDraft,assets"),
                            text=True, capture_output=True)
    if result.returncode:
        run("gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft",
            "--title", tag, "--notes", "Maven publications with SLSA build provenance and CycloneDX attestations.")
    else:
        # Existing assets are immutable: accept identical bytes; do not clobber conflicts.
        existing = work / "existing-assets"
        existing.mkdir()
        published_assets = json.loads(result.stdout)["assets"]
        if published_assets:
            run("gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", str(existing))
        for path in assets:
            previous = existing / path.name
            if previous.exists():
                if previous.read_bytes() != path.read_bytes():
                    raise ValueError(f"GitHub Release asset conflict: {path.name}")
        assets = [path for path in assets if not (existing / path.name).exists()]
    if assets:
        run("gh", "release", "upload", tag, "--repo", REPOSITORY, *(str(path) for path in assets))
    run("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false")
    summary(f"Release verified: https://github.com/{REPOSITORY}/releases/tag/{tag}")


def publish(root, version):
    verify(root, version)
    work = Path(tempfile.mkdtemp(prefix="publication-", dir=os.environ.get("RUNNER_TEMP")))
    # Keep deployment records and the exact signed bytes for publication retries.
    summary(f"Publication records: `{work}`")
    try:
        sign(root, version, work / "gnupg")
        if version.endswith("-SNAPSHOT"):
            publish_snapshots(root, version, work)
        else:
            publish_release(root, version, work)
            github_release(root, version, work)
    finally:
        subprocess.run(("gpgconf", "--homedir", str(work / "gnupg"), "--kill", "gpg-agent"), capture_output=True)
        shutil.rmtree(work / "gnupg", ignore_errors=True)
        (work / "settings.xml").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("context", "validate", "prepare", "verify", "publish", "artifacts"))
    parser.add_argument("root", type=Path)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--checksums-dir", type=Path)
    parser.add_argument("--module", choices=MODULES)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "context":
        context(root)
    elif args.command == "artifacts":
        artifact_ids()
    elif args.version is None:
        parser.error("This command requires a version")
    elif args.command == "prepare":
        if args.checksums_dir is None:
            parser.error("prepare requires --checksums-dir")
        prepare(root, args.version, args.checksums_dir.resolve())
    elif args.command == "validate":
        validate(root, args.version)
    elif args.command == "verify":
        verify(root, args.version, args.module)
    else:
        publish(root, args.version)


if __name__ == "__main__":
    main()
