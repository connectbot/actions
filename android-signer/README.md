# Android signer action

Shared metadata, verification, and publishing helpers for the Hermit Android
signer. Private Android keys stay on the signing appliance. This action runs
Python standard-library code and pinned Android reference tools; it does not
build Android applications or hold their signing keys.

Use `connectbot/actions/android-signer@FULL_COMMIT_SHA`. Keep the thin build,
attestation, finalization, and publishing workflows in the consuming repository,
with separate hosted jobs and minimal permissions. Keep its public signing
policy there too. Moving this composite action does not change reusable workflow
identities or Google OIDC conditions.

```yaml
- uses: connectbot/actions/android-signer@FULL_COMMIT_SHA
  env:
    GH_TOKEN: ${{ github.token }}
  with:
    command: prepare
    source-run: ${{ inputs.source_run }}
    source-attempt: ${{ inputs.source_attempt }}
    policy: .github/signer-policy.json
    out: ${{ runner.temp }}/verified-release
    tools: ${{ runner.temp }}/reference-tools
```

Operations:

| Command | Required inputs besides `command` and `out` |
| --- | --- |
| `build-info` | `profile`, `version-code-file` |
| `prepare` | `source-run`, `source-attempt`, `tools`; optional `policy` |
| `seal` | `bundle`; optional `policy` |
| `verify` | `manifest-sha256`; optional `policy` |
| `github` | `manifest-sha256`, `report`; optional `policy` |
| `play` | `manifest-sha256`, `report`; optional `policy` |

`policy` defaults to `.github/signer-policy.json`. `seal` emits
`manifest_sha256`. Callers provide Java 25 for `prepare`, GitHub CLI and
`GH_TOKEN` for network operations, and a short-lived `GOOGLE_PLAY_ACCESS_TOKEN`
for `play`. Verify the handoff with `verify` before requesting Google credentials.
Play publishing is restricted by the consumer workflow to its alpha environment;
the helper additionally enforces package `org.connectbot` and track `alpha`.

The signing appliance pins the exact caller/build workflow contents. The build
workflow's literal full action commit binds the action's Git tree, including
Python helpers, Java verifier, and tool digests. Updating the action revision in
that workflow requires review and a new appliance policy revision. Never replace
the action reference with a branch/tag or fetch helper scripts from mutable refs.

Run `python3 -m unittest discover -s android-signer/tests -v` from the actions
repository. Tests mock credentials and APIs and cover receipt/provenance tampering,
Android payload preservation, publishing recovery, and argument injection.
