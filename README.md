# ConnectBot actions

Shared GitHub Actions for ConnectBot releases, versioned documentation, and reviewed stale issues.

| Automation | Entry points | Guide |
| --- | --- | --- |
| Releases | `prepare/`, `publish/`, `branch/`; reusable release workflows | Setup and usage below |
| Documentation | `connectbot/actions/docs@REF` | [Documentation action](docs/README.md) |
| Stale issues | `connectbot/actions/stale-issues@REF`; reusable `stale-issues.yml` workflow | [Stale issue policy](stale-issues/README.md) |

Use a reviewed immutable commit in place of `REF`. Each consumer owns its triggers, permissions, and project configuration. See the [repository rename checklist](docs/rename-to-actions.md) when migrating existing release consumers.

## Releases

Issue-driven releases for Gradle projects. Prepares a release PR, then tags the release and advances the target branch after approval and passing CI. Your repository's CI publishes the artifacts.

## Set up your repository

- Install the release GitHub App with access to your repository and the organization's `.github` policy repository. Configure an [Octo STS](https://github.com/octo-sts/app) broker with the app's ID and private key, then add its hostname as an organization Actions variable named `OCTO_STS_DOMAIN` (for example, `octo-sts.example.com`). The private key stays only in the broker.
- In the `.github` policy repository, add Octo STS policies named `<repository>-release-prepare`, `<repository>-release-publish`, and `<repository>-release-branch`. Restrict them to the calling repository and the corresponding reusable workflow, and grant only the permissions listed below.
- Configure branch protection or rulesets to require pull requests and at least one CI check. A repository-wide required approval is optional; the release action independently requires a current-head approval from a collaborator with maintain or admin access. Allow the release app to bypass the pull-request rule, push to the target branch, and create release tags.
- Copy [release.yml](.github/ISSUE_TEMPLATE/release.yml) into your repository's `.github/ISSUE_TEMPLATE/` directory. Keep the field labels unchanged; the action uses them to read release inputs.
- Create the labels `release:prepare` and `release:publish`. For maintenance branches, also copy [release-branch.yml](.github/ISSUE_TEMPLATE/release-branch.yml) and create `release:branch`.

## Configure Gradle

The project must have a Gradle wrapper, the `net.researchgate.release` plugin, and a root `gradle.properties` containing `version=...`.

In `build.gradle.kts`, configure the release plugin to allow work branches and suppress pushes during preparation:

```kotlin
import net.researchgate.release.ReleaseExtension

configure<ReleaseExtension> {
    tagTemplate.set("v\${version}") // Use "\${version}" for unprefixed tags.
    buildTasks.set(listOf("build"))

    git {
        requireBranch.set("^(main|release/.+|release-work/.+)$")
        commitVersionFileOnly.set(true)
        if (providers.gradleProperty("release.noPush").isPresent) {
            pushToRemote.set(false)
        }
    }
}
```

## Add the release workflow

Save this as `.github/workflows/release.yml` on your repository's default branch:

```yaml
name: Release
on:
  issues:
    types: [labeled]

permissions:
  contents: read
  id-token: write
  issues: write

jobs:
  prepare:
    if: github.event.label.name == 'release:prepare'
    uses: connectbot/actions/.github/workflows/prepare-release.yml@main
    with:
      tag_prefix: "v"

  publish:
    if: github.event.label.name == 'release:publish'
    uses: connectbot/actions/.github/workflows/publish-release.yml@main
    with:
      tag_prefix: "v"

  # Optional: create maintenance branches using the Release branch issue form.
  branch:
    if: github.event.label.name == 'release:branch'
    uses: connectbot/actions/.github/workflows/release-branch.yml@main
```

Set `tag_prefix` to `""` in both jobs for unprefixed tags, and match it in Gradle's `tagTemplate`. The prepare workflow also accepts `java_version` (default `"17"`), `java_distribution` (default `"zulu"`), and `gradle_no_push_prop` (default `"release.noPush"`).

The Octo STS policies must issue tokens for only the target repository with these permissions:

| Identity | Permissions |
| --- | --- |
| `<repository>-release-prepare` | `contents: write`, `pull_requests: write`, `issues: write` |
| `<repository>-release-publish` | `contents: write`, `pull_requests: read`, `checks: read`, `statuses: read`, `issues: write` |
| `<repository>-release-branch` | `contents: write`, `issues: write` |

Each policy should exactly match the caller's OIDC subject and `audience: <OCTO_STS_DOMAIN>`, and constrain `job_workflow_ref` to its matching workflow in `connectbot/actions`. Put `repositories: [REPOSITORY]` in every policy. Also restrict organization issuers to `https://token.actions.githubusercontent.com` in `.github/chainguard/trusted-token-issuers.yaml`.

## Hook up artifact publishing

In your existing CI workflow:

- Run builds and tests on `pull_request` events, including drafts, so the release PR reports its required checks.
- Publish release artifacts only on tag pushes. Add a matching tag filter to your `push` trigger, such as `tags: ['v*']` for prefixed tags or `tags: ['[0-9]*']` for unprefixed tags.
- Publish snapshots only on pushes to `main` when the project version ends in `-SNAPSHOT`.

Use these conditions on your existing publishing steps:

```yaml
- name: Publish snapshot
  if: github.event_name == 'push' && github.ref == 'refs/heads/main' && endsWith(steps.project-version.outputs.version, '-SNAPSHOT')
  run: ./gradlew publishToMavenCentral

- name: Publish release
  if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')
  run: ./gradlew publishToMavenCentral
```

Replace `steps.project-version.outputs.version` with your CI's version output, and keep your existing publishing credentials and Gradle arguments.

## Check your setup

Run these read-only checks with `gh` authenticated as a repository admin. Replace `OWNER/REPO` and the workflow filename if yours differs:

```bash
release_repo=OWNER/REPO
gh auth status
gh api "repos/$release_repo" --jq '{has_issues, permissions}'
gh label list --repo "$release_repo" --search 'release:' --limit 100
gh workflow list --repo "$release_repo" --all
gh workflow view release.yml --repo "$release_repo" --yaml
```

Expect issues enabled, `permissions.push: true`, the release labels, and an active release workflow. Its YAML should contain the `issues: labeled` trigger and `contents: read`, `id-token: write`, and `issues: write` permissions, as above.

Check repository variables and, for organization-owned repositories, [organization variables shared with this repository](https://docs.github.com/en/rest/actions/variables#list-repository-organization-variables):

```bash
gh variable list --repo "$release_repo"
gh api "repos/$release_repo/actions/organization-variables" --paginate --jq '.variables[].name'
```

`OCTO_STS_DOMAIN` must appear across these lists. This verifies its presence, not broker reachability, the trust policies, or the app's installation and permissions.

Check the target branch's rulesets and classic branch protection (shown for `main`):

```bash
gh ruleset check main --repo "$release_repo"
gh api "repos/$release_repo/branches/main/protection"
```

Expect a pull-request rule and at least one required status check. The required approval count may be zero, which is useful for single-maintainer repositories. Inspect the applicable rules' bypass settings to ensure the release app can push directly; also check any tag rules. A `Branch not protected` response only means there is no classic protection—rulesets may still apply. Other access errors need resolving before you can assess the setup.

Once preparation creates a release PR, check it before adding `release:publish`:

```bash
release_pr=123 # Replace with the release PR number.
gh pr view "$release_pr" --repo "$release_repo" \
  --json url,state,isDraft,headRefOid,reviews,mergeable,mergeStateStatus
gh pr checks "$release_pr" --repo "$release_repo" --required
```

Expect `OPEN`, `isDraft: true`, `MERGEABLE`, an `APPROVED` review on the current `headRefOid` from a collaborator with maintain or admin access, and at least one required check, all passing. `DRAFT` is an expected merge state. These checks use your credentials; they cannot prove the release app can push or that artifact publishing will succeed. There is no dry-run release mode.

## Make a release

1. Open a **Release** issue with the release version (without a tag prefix), next development version (for example, `1.2.4-SNAPSHOT`), and target branch (`main` or `release/...`).
2. Add `release:prepare`. The action creates `release-work/<version>` and opens a draft PR.
3. Submit an **Approve** review and wait for all required checks to pass. Leave the PR open and in draft; do not merge it or mark it ready for review.
4. Add `release:publish` to the issue. The action tags the release commit and fast-forwards the target branch to the next development version. Your tag-triggered CI publishes the artifacts.

The person applying a release label must have maintain or admin access. To create a maintenance branch, open a **Release branch** issue and add `release:branch`.

Draft PRs disable GitHub's merge button but still allow reviews. Someone with write access can mark them ready, so this is an accidental-merge guard, not an access restriction. If publishing fails before pushing the tag, resolve the reported blockers and use **Re-run failed jobs**, or remove and reapply `release:publish`.

## Run tests locally

From the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 -m pip install --requirement docs/scripts/requirements.txt
python3 -m unittest discover -s docs/scripts/tests -v
node stale-issues/stale-issues.test.mts
```

The stale checker runs directly with Node 24 or newer. Each action has its own CI checks; documentation CI also exercises the composite action with a local source fixture.
