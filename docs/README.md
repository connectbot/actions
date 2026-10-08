# Versioned Kotlin documentation Action

Build and maintain a Dokka website for every supported release and the development branch. The site defaults to the latest published tag and includes a version selector, links to new and changed Kotlin APIs, syntax highlighting, and formatted changelog excerpts.

The publisher and shared defaults live here. Each calling repository owns its `.github/docs.json`, Gradle documentation configuration, and templates. See the [configuration and local CLI guide](configuration.md).

## Usage

Check out the source repository with complete history and tags, then set up the JDK, Gradle, and any SDK needed by its build. Call the Action to assemble the site:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    ref: main
    fetch-depth: 0
    fetch-tags: true

# Set up the JDK, Gradle, and project-specific SDKs here.

- name: Build documentation
  id: docs
  uses: connectbot/actions/docs@main
  env:
    GH_TOKEN: ${{ github.token }}
  with:
    publish: 'true'
    retry-failed: ${{ inputs.retry_failed || false }}
```

The Action sets up Python 3.12 and installs its dependencies into an isolated environment. It uses `github.action_path` to locate its publisher and defaults, independently of the source checkout. See GitHub's [composite Action metadata reference](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax#runs-for-composite-actions).

`publish` defaults to `false`. Setting it to `true` saves the generated Git archive with a fast-forward push. The caller supplies checkout credentials and `contents: write`. Configured CI admission also needs `actions: read` and `GH_TOKEN`. Pages deployment stays in the calling workflow, with its own `pages: write` and `id-token: write` permissions.

The publisher records incomplete versions in its outputs while retaining verified documentation. Callers can deploy when `ready == 'true'`, preserve logs with `if: always()`, and mark the job failed after deployment when `failures != '0'`. Use a concurrency group with `cancel-in-progress: false` to serialize archive updates.

Use an immutable Action commit in production after publishing this repository. `@main` is the initial integration reference; the Action must be published before the library workflows can resolve it.

## Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `repository` | `${{ github.workspace }}` | Source Git checkout with full history and tags. |
| `config` | `.github/docs.json` | Repository-relative or absolute config path. |
| `output` | `${{ runner.temp }}/docs-site` | New site directory outside the source checkout. |
| `logs` | `${{ runner.temp }}/docs-logs` | Build log directory. |
| `retry-failed` | `false` | Retry failed tagged builds; development builds retry automatically. |
| `require-ci` | `false` | Force CI admission for a build without publication. Publication also respects the config's CI requirement. |
| `publish` | `false` | Push the assembled Git archive. |

Boolean inputs accept `true` or `false`.

## Outputs

| Output | Meaning |
| --- | --- |
| `ready` | `true` when verified development documentation is available for deployment. |
| `failures` | Number of versions that are missing, failed, pending, or stale. |
| `output` | Absolute site directory. |
| `logs` | Absolute build log directory. |

## Local development

Run these commands from the `docs/` directory in this repository.

```sh
python3 -m pip install --requirement scripts/requirements.txt
python3 -m unittest discover -s scripts/tests -v
```

Preview an assembled site with explicit UTF-8 headers so browsers display Unicode in raw Markdown correctly:

```sh
python3 scripts/preview.py --directory /tmp/documentation-site --port 8765
```

For a local Action test, check this repository out at a separate path in the workflow and use that path in `uses: ./path/to/cb-actions/docs`. The publisher CLI can also run directly from this checkout against any library repository; see the [guide](configuration.md#running-locally-or-from-an-action).

The regression fixtures cover Android documentation, a single JVM module, an aggregate JVM publication, archive retention and retry, CI admission, API reports, changelogs, and an installation outside the source repository.

Licensed under [Apache 2.0](../LICENSE).
