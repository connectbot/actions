# Verify GitHub Actions lockfiles

The reusable `.github/workflows/verify-lockfile.yml` workflow checks the calling
repository's `.github/workflows/actions.lock` with `gh actions-lock --verify`.
It fails when the lockfile is missing, incomplete, or a locked dependency no longer
verifies upstream. Verification does not update the workflows or lockfile.

Add this job to a consumer workflow, replacing `REF` with a reviewed commit from
`connectbot/actions` that includes the verifier:

```yaml
jobs:
  lockfile:
    name: Verify Actions lockfile
    uses: connectbot/actions/.github/workflows/verify-lockfile.yml@REF
    permissions:
      contents: read
```

The consumer owns the triggers. Run the job on pull requests and `merge_group`
events when using a merge queue, and include it in any aggregate required check.
Push and scheduled runs also catch upstream changes between pull requests. No
additional secrets or inputs are required; the workflow uses the caller's
`github.token` and checks out the caller's revision with credentials persistence
disabled.

After adding or changing the caller, run `gh actions-lock` in the consumer
repository and commit the generated lockfile. To fix a verification failure,
inspect the reported dependency changes before regenerating the lockfile.

The verifier's CLI version is pinned in the shared workflow and tracked by this
repository's Renovate custom manager for `gh extension install OWNER/REPO --pin
VERSION`. Consumers update their reference to the shared workflow to receive new
verifier versions.
