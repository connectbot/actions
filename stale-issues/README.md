# Reviewed stale issues

An issue is eligible only after a human owner, member, or collaborator has commented. Unreviewed issues are skipped indefinitely. Reviewed issues receive a stale label and warning after 180 days without activity, then close after another 30 days without activity. New activity removes the stale label and restarts the cycle.

Issues with milestones, the `keep-open` label, locked issues, and pull requests are exempt. Creating an issue as a member does not count as review; a member comment is required. Closure requires this action's own warning comment, not just a manually applied stale label.

## Scheduled usage

Save this workflow in each repository that should use the policy. The shared workflow targets the calling repository; scheduling and concurrency belong to the caller.

```yaml
name: Close reviewed stale issues
on:
  schedule:
    - cron: '17 4 * * *'
  workflow_dispatch:
    inputs:
      dry_run:
        description: Preview decisions without changing issues
        type: boolean
        default: true
permissions:
  contents: read
  issues: write
concurrency:
  group: stale-issues
  cancel-in-progress: false
jobs:
  stale:
    uses: connectbot/actions/.github/workflows/stale-issues.yml@main
    with:
      dry_run: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || false }}
```

Use a reviewed immutable commit instead of `@main` for production. A manual run defaults to preview mode; scheduled runs change issues. The reusable workflow itself defaults to preview mode when its input is omitted.

## Composite action

Call `connectbot/actions/stale-issues@REF` from a job with `issues: write`. It needs no caller checkout. Inputs are `github-token` (defaults to `github.token`) and `dry-run` (defaults to `true`). Its `summary` output is JSON with `mark`, `close`, `unmark`, and `skip` counts.

The script runs directly as TypeScript on the Node 24 runtime supplied by `actions/github-script`. There is no build step or dependency installation.

## Tests

From the repository root, with Node 24 or newer:

```sh
node stale-issues/stale-issues.test.mts
```
