# Android signer action

Shared metadata, verification, and publishing helpers for the Hermit Android
signer. Private Android keys stay on the signing appliance. This action runs
Python standard-library code and pinned Android reference tools; it does not
build Android applications or hold their signing keys.

Use the trusted reusable workflows below for attestation, finalization, and
publication. Their policy loader always reads the centrally governed
`connectbot/.github` repository; callers cannot replace the policy source.
The composite action provides metadata and verification operations to these
workflows. Application build jobs use only its `build-info` operation.

The signer, unsigned attestor, and CI share `android-signer/runtime/action.yml`.
It uses SHA-pinned `mise-action` and mise 2026.10.9 to install Python 3.14.7
and, for finalization, Zulu 25.36.205.0 (JDK 25.0.4.1) from `runtime/mise.toml` and
`runtime/mise.lock`. Installs require the lockfile and its archive checksums;
Python source builds and restored tool caches are disabled. The action exports
the installed tool directories to PATH without enabling caller-dependent mise
shims. The reusable workflows and CI select Ubuntu 24.04; hosted image patches
still update within that Ubuntu release.

The shared dependency installer pins pip 26.0.1 by wheel hash, then installs
exact-version, hash-pinned trust dependencies in a fresh virtual environment.
Source builds and implicit dependency resolution are disabled, and `pip check`
verifies the dependency closure. Update the mise configuration, generated
lockfile, and wheel hashes together; CI installs the same runtime and dependencies
before running the verifier tests.

Operations:

| Command | Required inputs besides `command` and `out` |
| --- | --- |
| `build-info` | `profile`, `version-code-file` |
| `prepare` | `source-run`, `source-attempt`, `tools`, `policy` |
| `seal` | `bundle`, `policy` |
| `verify` | `manifest-sha256`, `policy` |
| `github` | `manifest-sha256`, `report`, `policy` |
| `play` | `manifest-sha256`, `report`, `policy` |

The `policy` operation writes the central policy snapshot. All verification and
publication operations require its explicit path; there is no local policy
fallback. `seal` emits
`manifest_sha256`. The composite action supplies the built-in `github.token` as
`GH_TOKEN`, including for public central policy reads, to avoid GitHub's shared
anonymous IP rate limit. No additional token secret is needed. Direct script
invocations must supply `GH_TOKEN` for network operations and Java 25 for
`prepare`; the composite action supplies Java. Callers provide GitHub CLI and a
short-lived `GOOGLE_PLAY_ACCESS_TOKEN` for `play`.
Verify the handoff with `verify` before requesting Google credentials.
Play publishing is restricted by the consumer workflow to its alpha environment;
the helper additionally enforces package `org.connectbot` and track `alpha`.

Run `python3 -m unittest discover -s android-signer/tests -v` from the actions
repository. Tests mock credentials and APIs and cover receipt/provenance tampering,
Android payload preservation, publishing recovery, and argument injection.


## Trusted reusable workflows

Keep application builds in the consuming repository: Java/SDK setup, Gradle tasks,
flavor matrices, output paths, and release metadata belong beside the application.
Build jobs must have only `contents: read` permission and no signing secrets or
provenance credentials. Use fresh hosted runners and disable shared build caches.
The `build-info` composite command can create release metadata alongside the build.

After the build succeeds, call
`connectbot/actions/.github/workflows/android-attest.yml@android-signer-v1` once
per profile:

```yaml
attest-unsigned:
  needs: release
  if: ${{ github.event_name == 'push' }}
  strategy:
    matrix:
      flavor: [oss, google]
  uses: connectbot/actions/.github/workflows/android-attest.yml@android-signer-v1
  permissions:
    contents: read
    actions: read
    id-token: write
    attestations: write
  with:
    profile: ${{ matrix.flavor }}
```

The build must upload `unsigned-PROFILE-ATTEMPT` containing `app.apk` and `app.aab`,
and `release-support-PROFILE-ATTEMPT` containing `mapping.txt`,
`native-debug-symbols.zip`, and `build-info.json`, with flat paths. `ATTEMPT` is
`${{ github.run_attempt }}`. This is a stable handoff contract, independent of
Gradle task names, project layout, Java versions, and original output filenames.
A new profile additionally needs independently provisioned signing policy/keys.

The shared attestor accepts only a profile label. It derives repository, run and
attempt from GitHub, downloads that run's immutable artifacts, checks their file
sets, and computes the subject digests on a fresh hosted runner. It accepts no
caller commands, artifact IDs, source overrides, or signing digests and never
checks out or executes application code. Provenance authenticates the source run
and output bytes; it does not certify the application's build logic as safe.

The finalizer and publishers are `android-finalize.yml` and `android-publish.yml`
at the same protected branch. They load the public policy from immutable
repository ID 1215286979 (`connectbot/.github`), protected `main`, at
`.github/android-signer/REPOSITORY_ID.json`. The source repository cannot supply a
replacement policy. The resolved policy commit/digest and builder revision are
recorded in final provenance. Google credentials are requested only after handoff
verification. Keep the policy repository and `android-signer-v1` protected.

The VM authorizes the builder repository ID, workflow
path and protected branch. Caller paths still identify eligible CI entry points,
as a path allowlist. Key identities, source refs, package names,
rotation constraints, run/attempt and artifact digests remain enforced. Routine
caller edits and reviewed updates on the trusted branch do not change VM policy.
Only trusted reusable-workflow authorization is supported. Version 3 policy
requires explicit builder/finalizer identities and contains no authorization mode,
caller-file digests, or workflow/helper content pins.


## Public releases and verification evidence

Each completed source build leaves one public GitHub release. Its APK/AAB files
keep `ConnectBot-<version>-<profile>` names. Development releases have six assets:
four Android files, `version_code.txt`, and
`ConnectBot-<version>-signing-evidence.zip`. Stable releases also keep
`version-info.txt` for F-Droid.

The archive contains the original signed manifest and Sigstore bundle, receipts,
provenance, transparency/rotation evidence, mappings, and native debug symbols.
These are the original authenticated files; the ZIP is a transport container,
not a separately signed statement. Verify its contents against the signed
manifest and attestation. To verify a downloaded APK, read `final.sigstore.json`
from the evidence archive and run `gh attestation verify` with the consuming
repository and trusted `connectbot/actions/.github/workflows/android-finalize.yml`
workflow identity.

The finalizer seals an immutable Actions handoff without creating a verification
release. After publication verifies every public download and the complete archive,
the publisher removes only receipt-matching staging drafts for that source
run/attempt/policy. Retrying after cleanup recovers the original signed outputs
from the public evidence archive and rechecks their receipts and digests. Failed
Play uploads retry from the immutable handoff and do not require new signatures.
Original build artifacts must still be available when re-running full finalization.


## Parallel ConnectBot release lines

The verified release source selects the Play testing track. Exact stable
`v1.11.x` tags (patch 0–49, with the matching stable Android code) publish to
`beta`; other eligible releases retain `alpha`. The `play-route` operation
verifies the complete handoff before emitting `play_track`. `verify-play` and
`play` require a `play-track` input and reject destinations that disagree with
the verified source. Production is not an accepted destination.

Enable `PLAY_ALPHA_ENABLED` and/or `PLAY_BETA_ENABLED` in the consuming repository.
Jobs use the corresponding `play-alpha` or `play-beta` environment and acquire
credentials only after verification and destination validation. Both use the
same application-wide publishing concurrency group. Superseded-release checks
read only the destination track, so a newer alpha does not block a maintenance
upload to beta. Promote tested beta bundles through Play Console.

An exact stable or alpha version tag at a main build's commit supersedes that
main publication, including GitHub publication. Generated `git-*` release tags
do not suppress new builds. Push a cutover branch and its annotated anchor in
the same atomic push; adding a tag after publication cannot retroactively undo
an already published main build.

For `build-info`, pass `version-name-file` alongside `version-code-file` to use
Gradle's base version name for main release labels. This keeps labels aligned
with the configured tag filter when maintenance tags enter main's history.
Older callers that omit this optional input retain the legacy `git describe`
behavior. Exact source tags remain authoritative for tagged releases.
