# Isolated SonarCloud analysis

Analyze fork PRs without exposing `SONAR_TOKEN` to their builds. The producer builds
and tests without Sonar credentials; a default-branch `workflow_run` consumer calls
this reusable workflow on a fresh GitHub-hosted runner. Only the standalone scanner
receives the token. No project build scripts or artifact executables run there.

Version one supports public GitHub repositories, SonarCloud's `sonarcloud.io`
region, and prebuilt JVM/Python projects. Other language build integrations,
private source repositories, merge queues, and SonarQube Server are not supported.

## Consumer workflow

Put this on the consuming repository's default branch, replacing `REVIEWED_SHA`
with an immutable commit of `connectbot/actions` containing the workflow:

```yaml
name: SonarCloud
on:
  workflow_run:
    workflows: [Continuous Integration]
    types: [completed]
permissions:
  contents: read
  actions: read
  pull-requests: read
jobs:
  sonar:
    uses: connectbot/actions/.github/workflows/sonar-analysis.yml@REVIEWED_SHA
    with:
      producer_workflow: .github/workflows/ci.yml
      artifact_prefix: sonar-inputs
      config_path: .github/sonar.json
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

All three inputs have the defaults shown. The project must be bound to the
SonarQube Cloud GitHub App, with PR analysis available for its target branches.
Require the App's **SonarCloud Code Analysis** check in branch rules. The consumer's
workflow check belongs to the default-branch context, so it is not a replacement
for the App's PR-head check. No GitHub write permission is needed.

## Producer contract

On PRs, build the exact `github.event.pull_request.head.sha`; on branch pushes and
scheduled runs, build `github.sha`. Keep separate merge-commit tests if desired.
Generate all reports from that same checkout. Never pass `SONAR_TOKEN` to the
producer, including same-repository PR builds.

Upload one artifact, named `sonar-inputs-${{ github.run_attempt }}`, with paths
relative to a staging directory containing only:

- `classes/`: compiled main/test `.class` files, arranged by module and source set.
- `libraries/`: main/test dependency `.jar` files, arranged by module and source set.
- `reports/`: JaCoCo-compatible and/or Python coverage `.xml` reports.

Directories are allowed. Symlinks, special files, executables, scanner settings,
and other extensions are rejected. Names must be normalized relative paths,
without backslashes, commas, control characters, or traversal components. Limits
are 512 MiB compressed, 1 GiB expanded, 128 MiB per file, and 50,000 entries.
Do not upload Gradle caches, a workspace archive, or generated source trees.
Source is fetched independently from GitHub; source symlinks and submodules are
also rejected. Reports must use repository-relative source paths. A standard
JaCoCo DOCTYPE is removed; other DTDs, entities, namespaces, and escaping source
paths are rejected.

Only successful runs are analyzed. Upload inputs for PRs, default/release branch
pushes, and scheduled default-branch builds. Other events and branches are skipped.
For example:

```yaml
- uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
  with:
    name: sonar-inputs-${{ github.run_attempt }}
    path: build/sonar-inputs/
    if-no-files-found: error
    retention-days: 7
```

## Trusted configuration

The consumer reads this file from its own default-branch workflow revision, never
from the PR or artifact. Configuration changes therefore take effect after merging.

```json
{
  "project_key": "organization_project",
  "organization": "organization",
  "properties": {
    "sonar.sources": "src/main",
    "sonar.tests": "src/test",
    "sonar.java.source": "17",
    "sonar.sourceEncoding": "UTF-8"
  },
  "artifact_paths": {
    "sonar.java.binaries": ["classes/*/main"],
    "sonar.java.test.binaries": ["classes/*/test"],
    "sonar.java.libraries": ["libraries/*/main/*.jar"],
    "sonar.java.test.libraries": ["libraries/*/test/*.jar"],
    "sonar.coverage.jacoco.xmlReportPaths": ["reports/kover/report.xml"]
  }
}
```

Omit irrelevant optional mappings, rather than supplying empty lists. Every
configured artifact pattern must match. Python coverage uses
`sonar.python.coverage.reportPaths` mapped under `reports/`. Source/test paths are
comma-separated repository-relative paths, not globs. `properties` allows source
and test inclusions/exclusions, coverage/duplication exclusions, source versions,
project display name, encoding, and issue-ignore criteria; `scan.py`'s `PROPERTIES`
and `ARTIFACT_PATHS` define the complete allowlists. Authentication, host,
executables, working directories, PR identity, revision, and gate settings cannot
be overridden. There is no arbitrary-arguments or command hook.

## Security and failure behavior

The helper validates repository, producer workflow ID/path, run ID, attempt,
conclusion, PR head repository/branch/SHA, and artifact identity through GitHub's
API. Forks with empty PR associations are resolved through the commit-to-PR API;
ambiguous matches fail. It rechecks the current head before submitting analysis.
Closed PRs and superseded runs skip without manufacturing a passing check. Missing,
expired, duplicate, oversized, or invalid artifacts fail validation.

The scanner is checksum-pinned and uses fresh caches, an explicit external settings
file, a minimal environment, and independently fetched source. Project settings,
Java agents, hooks, restored caches, and Git credentials are not inherited. Scanner
logs redact the token and neutralize workflow commands. The scanner waits up to
300 seconds for the actual SonarCloud gate; timeout or rejection fails the job.

This trusts GitHub runners, the pinned scanner, and SonarCloud's analyzers. It
prevents PR-controlled commands/configuration from receiving credentials; it is
not a sandbox against vulnerabilities in trusted analyzers. Contributor-generated
coverage and bytecode are untrusted evidence: this does not prove their accuracy.
GitHub's first-time-contributor workflow approval policy still applies.

A PR update during analysis can leave an analysis for the old SHA. Explicit SCM
revision binds its result to that SHA; the new head still requires its own check.
Re-run **all producer jobs** to retry missing/expired artifacts or an interrupted scan.
Re-running only failed jobs may leave the artifact on an older attempt, which is rejected.
Failed builds already block on their normal required checks and are not scanned.

## Verification and rollout

Run `python3 -m unittest discover -s sonar/tests -v` and actionlint. Set
`SONAR_SCANNER_INTEGRATION=1` with Java 21 in `JAVA_HOME` to also download the pinned
scanner and verify that it ignores hostile PR settings; CI runs this smoke test
without credentials or a server upload. Publish the
reviewed shared-workflow commit before pinning consumers to it. After the consumer
lands on its default branch, verify a fork PR receives the App check for its head
SHA, including a gate failure that blocks merging. Compare analyzed files and
coverage at the same commit when migrating from Gradle scanning.

Renovate updates `SCANNER_VERSION` and `SCANNER_SHA256` together through the regex
manager in `renovate.json`. Its custom datasource reads Homebrew's published
[scanner metadata](https://formulae.brew.sh/api/formula/sonar-scanner.json), which
contains the version and SHA-256 of the upstream, platform-independent ZIP.
Updates become available when Homebrew publishes that metadata. The transform
rejects entries whose URL is not the exact versioned SonarSource ZIP or whose
checksum is malformed. Scanner downloads still come directly from SonarSource;
the runtime never fetches a checksum or accepts an unpinned version.

Scanner updates require review (`automerge: false`). The real-scanner smoke test
runs on the update PR and checks both the archive checksum and configuration
isolation. For a manual update, change both constants using the official
SonarSource distribution and run the same tests.
