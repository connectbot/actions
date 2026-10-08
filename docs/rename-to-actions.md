# Migrate to connectbot/actions

The action directories and reusable workflow filenames stay unchanged. This migration changes the GitHub repository name; it does not change release behavior or token permissions.

## References to update

Replace `connectbot/release-action` with `connectbot/actions` in consuming workflows:

| Interface | New reference |
| --- | --- |
| Prepare a release | `connectbot/actions/.github/workflows/prepare-release.yml@REF` |
| Publish a release | `connectbot/actions/.github/workflows/publish-release.yml@REF` |
| Create a maintenance branch | `connectbot/actions/.github/workflows/release-branch.yml@REF` |
| Prepare composite action | `connectbot/actions/prepare@REF` |
| Publish composite action | `connectbot/actions/publish@REF` |
| Branch composite action | `connectbot/actions/branch@REF` |

Keep the existing commit or tag in place of `REF`. A rename retains Git history. For new consumers, pin to a reviewed immutable commit. The reusable workflows use `$/prepare`, `$/publish`, and `$/branch` to load actions from their own repository and revision; these references need no change.

## Octo STS policies

For each consumer, update the trusted `job_workflow_ref` repository in its policies in `connectbot/.github`:

- `<repository>-release-prepare`: `connectbot/actions/.github/workflows/prepare-release.yml@REF`
- `<repository>-release-publish`: `connectbot/actions/.github/workflows/publish-release.yml@REF`
- `<repository>-release-branch`: `connectbot/actions/.github/workflows/release-branch.yml@REF`

Preserve each policy's existing ref constraints, caller subject, audience, repository scope, identity, and permissions. Confirm the release app and broker still have access to the renamed repository where required.

## Migration sequence

1. Run `python3 -m unittest discover -s tests -v` and `actionlint` on this preparation change, then publish it to the existing repository. Older actionlint versions reject GitHub's supported `$/` self-repository syntax; use a version that supports it or exclude only that specific diagnostic.
2. Find every consumer reference and prepare the caller and Octo STS policy updates together. Pause release requests during the cutover.
3. Rename `connectbot/release-action` to `connectbot/actions` in GitHub, then apply the prepared caller and trust-policy updates before resuming releases.
4. Update local clones with `git remote set-url origin git@github.com:connectbot/actions.git`. Renaming the local checkout directory is optional.
5. Verify workflow references resolve, the app installation remains accessible, and all three policy references match the new reusable workflow locations. Use the read-only setup checks in the README before the next planned release.

GitHub does not redirect action calls after a repository rename. Existing consumers must be updated explicitly; ordinary Git URL redirects are not sufficient. If uninterrupted action availability is required, GitHub recommends creating the new repository and archiving the old one after consumers migrate instead of renaming it. See [GitHub's repository rename documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).
