# Attested Maven publication

The reusable workflow `.github/workflows/publish-maven.yml` builds Gradle JVM
libraries, generates SLSA provenance and CycloneDX attestations in separate jobs,
then signs and publishes those exact bytes. It targets SLSA v1.0 Build Level 3
using GitHub-hosted runners and GitHub's reusable-workflow attestation model.
Confirm the first published-artifact verification run before claiming that level
for a consuming library. Earlier releases are not retroactively covered.

## Consumer contract

The consumer owns its CI triggers and checks. Invoke this workflow only after
all required source verification, workflow checks and library tests pass.

```yaml
publication:
  needs: [build, workflow-tests]
  if: >-
    github.event_name == 'push' &&
    (github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/heads/release/') ||
     startsWith(github.ref, 'refs/tags/v'))
  permissions:
    contents: write
    actions: read
    id-token: write
    attestations: write
  uses: connectbot/actions/.github/workflows/publish-maven.yml@REVIEWED_COMMIT_SHA
  with:
    maven_group: org.example.library
    modules: protocol,library
  secrets:
    MAVEN_GPG_PRIVATE_KEY: ${{ secrets.MAVEN_GPG_PRIVATE_KEY }}
    MAVEN_GPG_KEYID: ${{ secrets.MAVEN_GPG_KEYID }}
    MAVEN_GPG_PASSPHRASE: ${{ secrets.MAVEN_GPG_PASSPHRASE }}
    SONATYPE_USERNAME: ${{ secrets.SONATYPE_USERNAME }}
    SONATYPE_PASSWORD: ${{ secrets.SONATYPE_PASSWORD }}
```

Always pin the reviewed **shared builder commit**, not the library's source
commit or a mutable branch. The workflow uses GitHub.com's `job.workflow_sha`
to check out its own helpers at that exact revision. The source checkout is the
calling repository's triggering commit. No caller-supplied builder revision,
source revision, commands, artifact selectors or Gradle flags are accepted.

Inputs are `maven_group`, a comma-separated list of unique artifact IDs in
`modules`, `caller_workflow` (default `.github/workflows/ci.yml`), and
`tag_prefix` (default `v`, also accepts an empty prefix). The default calling
workflow must match unless explicitly configured otherwise. Snapshots publish
only on pushes to `main` or `release/X.Y`; releases require a matching annotated
tag reachable from one of those branches. Other branch names are rejected.

The source needs a Gradle wrapper and one `version=...` line in
`gradle.properties`. The fixed Java 17 build command is:

```sh
./gradlew --dependency-verification strict --no-build-cache stageMavenPublication
```

The source owns this staging task. It must generate exactly these nonempty,
unsigned files under `build/publication/ARTIFACT_ID/` for each configured module:

```text
ARTIFACT_ID-VERSION.jar
ARTIFACT_ID-VERSION-sources.jar
ARTIFACT_ID-VERSION-javadoc.jar
ARTIFACT_ID-VERSION.pom
ARTIFACT_ID-VERSION.module
ARTIFACT_ID-VERSION-cyclonedx.json
```

The POM and Gradle metadata must match the configured group, module and version.
Metadata file references and digests must match staged payloads. The SBOM must be
CycloneDX JSON. Unknown files, missing files, symlinks and mismatched metadata
stop attestation. New publication types require a reviewed builder change.

## Evidence and publishing

Each module gets a SLSA provenance v1 Sigstore bundle covering all six files, and
a CycloneDX attestation binding its binary JAR to the SBOM. Maven publishes the
bundles with `provenance` and `sbom-attestation` classifiers and `json` extension.
They are sidecars, avoiding circular digests. The original files retain their
exact bytes, including POM and Gradle metadata.

Releases use Central Portal's bundle API with GPG signatures and required
checksums. After Central reaches `PUBLISHED`, downloads are compared against the
attested files. A GitHub Release publishes the same payloads, bundles, signatures
and a SHA-256 manifest; it remains in draft until all assets are uploaded.

Snapshots use Maven GPG Plugin 3.2.8's standalone `sign-and-deploy-file` goal.
It preserves multipart signature extensions and deploys every classifier and
signature together with one timestamp/build number per module. The workflow
downloads and checks every payload and bundle. Snapshots use GitHub attestation
lookup and workflow artifacts, without creating snapshot GitHub Releases.

No signing or Sonatype credentials enter the build or attestation environments.
Only attestation jobs have OIDC permission. The publisher has upload credentials
but no OIDC permission, and never runs the source Gradle build. Each job uses a
fresh hosted runner; publication Gradle caches are neither restored nor saved.
Immutable build artifacts are selected from the same run by ID. Matrix output
collection retains the original build-attempt prefix when retrying failed jobs.
Publication is serialized per source repository, across branches.

The builder relies on GitHub Actions' control plane, hosted-runner isolation,
artifact transport, OIDC, and Sigstore. Network dependency downloads are allowed;
Gradle dependency verification is strict. The build is not claimed to be hermetic
or reproducible. Dependency SBOMs do not confer a SLSA level on third-party code.

## Verification and downstream policy

Consumers must enforce both the **source repository/commit** and the **shared
signer workflow/revision**. A library's source commit and builder commit differ:

```sh
gh attestation verify library-VERSION.jar \
  --bundle library-VERSION-provenance.json \
  --repo OWNER/LIBRARY \
  --signer-workflow connectbot/actions/.github/workflows/publish-maven.yml \
  --signer-digest REVIEWED_SHARED_BUILDER_COMMIT \
  --source-digest EXPECTED_LIBRARY_SOURCE_COMMIT \
  --source-ref refs/tags/vVERSION \
  --deny-self-hosted-runners
```

The signer workflow flag handles cross-repository signers. For online discovery,
omit `--bundle`. For SBOM verification, use the `sbom-attestation` bundle and
`--predicate-type https://cyclonedx.org/bom`. For snapshots, retrieve matching
timestamped files and enforce the expected branch ref and source commit.
Use `--format json` for additional policy checks on verified statements.

Downstream builds can retain these bundles and verification results, and record
the dependency's Maven coordinates and digest in their own provenance
`resolvedDependencies`. Their own SLSA level depends on their builder and policy.

## Testing and recovery

```sh
python3 -m unittest discover -s publication/tests -v
PUBLICATION_INTEGRATION=1 python3 -m unittest discover -s publication/tests -v
actionlint .github/workflows/publish-maven.yml .github/workflows/publication-ci.yml
```

The integration test needs Java, Maven, GPG and localhost sockets. It creates a
disposable protected key and a temporary HTTP Maven repository, then verifies
both snapshot metadata and signatures. No real artifacts are published.
The actionlint configuration ignores only its outdated schema's rejection of
GitHub.com's documented `job.workflow_sha` property.

Workflow summaries link attestations and Central deployment IDs. Publication
records retain deployment IDs and uploaded bundles for 30 days; attested workflow
artifacts also last 30 days. Sonatype's snapshot retention is independent.
For recovery, rerun the failed publisher to reuse the same attested files.
Regenerating attestations changes bundle bytes and can conflict with an immutable
release. Published release bytes and GitHub assets are never silently replaced.
Inspect recorded Central deployments before retrying an interrupted upload.

References: [SLSA v1.0 requirements](https://slsa.dev/spec/v1.0/requirements),
[GitHub Level 3 guidance](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/increase-security-rating),
[GitHub job identity context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context).
