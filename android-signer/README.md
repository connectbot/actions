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
`manifest_sha256`. Callers provide Java 25 for `prepare`, GitHub CLI and
`GH_TOKEN` for network operations, and a short-lived `GOOGLE_PLAY_ACCESS_TOKEN`
for `play`. Verify the handoff with `verify` before requesting Google credentials.
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
