# Versioned documentation publisher

The publisher builds a versioned Dokka site, adds a version selector and public API change reports, and maintains a generated Git archive. The publisher and its tests are maintained in this Action repository. Project settings live in `.github/docs.json`. Shared defaults live alongside the publisher in `docs.defaults.json`; Gradle configuration and templates remain repository-owned build inputs.

## Running locally or from an Action

Use Python 3.12 or later, Git, and the tools required by the configured build command. These repositories use JDK 17 and the Gradle wrapper; termlib also needs an Android SDK. CI admission requires the GitHub CLI and `GH_TOKEN` with permission to read workflow runs and jobs.

Install the publisher’s dependencies, then run from any directory, including with the publisher installed outside the checkout:

```sh
python3 -m pip install --requirement /path/to/publisher/requirements.txt
```

```sh
python3 /path/to/publisher/publish-docs.py \
  --repo /path/to/source \
  --config .github/docs.json \
  --output /tmp/documentation-site \
  --logs /tmp/documentation-logs
```

Serve the built output with `python3 /path/to/publisher/preview.py --directory /tmp/documentation-site --port 8765`. This preview server declares UTF-8 for text files, including raw Markdown; a plain `python3 -m http.server` can leave browsers guessing a different encoding.

The source checkout must contain its development branch and complete tag history. Relative config paths resolve against `--repo`. The output directory must be new and outside the source checkout. Source files and the Git index remain untouched; build adapters operate in temporary detached clones.

Add `--publish` to push the assembled archive to the configured remote and branch. Publishing uses an ordinary fast-forward push and leaves Pages deployment to the caller. Without `--publish`, no archive branch is pushed. `--retry-failed true` retries previously failed tagged builds. Development builds retry automatically. `--require-ci` applies configured CI admission to every source commit; repositories with `ci.required_for_publish` also enforce it whenever publishing.

When `GITHUB_OUTPUT` is set, the publisher writes `ready` and `failures`. `ready` requires verified development documentation; `failures` counts versions that are missing, failed, pending, or stale. Successful versions can still be archived while other builds fail. When `GITHUB_STEP_SUMMARY` is set, the publisher appends a per-version summary. Build logs stay in the supplied logs directory, outside the public archive. The caller sets up JDK/SDK/Gradle, supplies credentials, uploads logs, and deploys the output through Pages.

## Configuration schema 1

A typical repository only needs its identity and documentation modules:

```json
{
  "schema": 1,
  "site": {"title": "Example library", "base_url": "https://docs.example.org"},
  "repository": {"slug": "example/library"},
  "build": {"project": "library"},
  "api": {"library": "Library"}
}
```

The publisher loads `docs.defaults.json` from its own installation directory, then applies repository overrides. Objects merge recursively; lists and scalar values replace defaults. Explicit values, including `null` where supported, take precedence. Both concise and fully expanded configs are accepted.

The defaults select `main`, use its name as the selector label, derive the repository URL from the GitHub slug, archive to `origin/gh-pages` as the Actions bot, include every SemVer tag, and look for `CHANGELOG.md`. Dokka builds use the Gradle wrapper, HTML and Markdown tasks, a 30-minute timeout, and the standard `build/dokka` output directories. CI is disabled unless a `ci` section is supplied; its defaults then select push runs of `ci.yml` and require admission when publishing. All these values are in the shared JSON file and can be overridden.

| Section | Repository settings |
| --- | --- |
| `site` | Required `title` and `base_url`; the public URL may include a path prefix. |
| `repository` | Required GitHub `slug`; optional `url`, `development_ref`, and `development_label` overrides. |
| `archive` | Optional overrides for `remote`, `branch`, `author_name`, and `author_email`. |
| `tags` | Optional `minimum` SemVer; omitted or `null` admits every valid SemVer tag. |
| `build` | Dokka `project` directory, plus any build exceptions described below. Omit `project` or use `.` for the root project. |
| `api` | A map of module directory to display label; paths default to `<module>/api.txt` and `<module>/src/main`. The expanded list of `label`, `path`, and `source_path` objects is also accepted. |
| `changelog` | Optional repository-relative path override; `null` disables excerpts. |
| `ci` | Required `jobs` when enabled; optional `workflow`, `event`, and `required_for_publish` overrides. |

Concise Dokka build settings:

- `project`: repository-relative Gradle project directory; also determines task prefixes and output paths. Nested projects use directories such as `libraries/core`.
- `arguments`: additional Gradle arguments, appended to the shared arguments. For example, `["--dependency-verification", "strict"]`.
- `probe`: an alternate Gradle file to check for historical Dokka support, or an object with `path` and `pattern` overrides. It defaults to the documentation project's `build.gradle.kts`.
- `init_script`: a repository-relative Gradle init script. The adapter passes the documentation project as `docsModule` and includes the script in the build fingerprint.
- `gradle_overlay`: `true` copies the marked documentation block from the current project's Gradle file into historical checkouts, using the shared markers and imports. A different file path or the full settings object is also accepted.
- `overlays`: paths to copy into historical checkouts. A string means the source and target are identical; objects can set a different `target` or `optional: true`.
- `inputs`: additional current-checkout files or directories that affect documentation. Init scripts, marked Gradle blocks, and required overlays are included automatically. Optional local environment files are not automatically fingerprinted.

Repository paths must be relative and stay within the checkout. Build inputs must exist in the current checkout. The checked-in configs demonstrate Android, one JVM module, and an aggregate JVM publication. Advanced build fields below remain available for custom adapters and unusual layouts.

`build.command` runs without a shell. Each argument may contain these placeholders:

| Placeholder | Value |
| --- | --- |
| `{repo}` | Absolute current source repository, containing reviewed configuration and overlays. |
| `{publisher}` | Absolute directory containing the portable Python code. |
| `{checkout}` | Temporary clone checked out at the version being built. |
| `{output}` | New directory where the adapter must assemble a publication. |
| `{config}` | Temporary JSON file containing the validated current configuration. |
| `{commit}`, `{version}` | Exact source commit and displayed branch/tag name. |
| `{repository_url}` | Configured source repository URL. |

A custom adapter must exit successfully only after producing `index.html` and `index.md`, plus the full HTML/Markdown documentation tree. Nonzero exit, timeout, missing entrypoints, or symlinks fail that version without replacing its previous verified tree. Source links must reference the supplied commit. Adapters must enforce public-only documentation, matching the archive's visibility verification policy.

The supplied `docs_build.py` adapter uses additional external `build` settings:

- `gradle_command`: wrapper tasks/options as an argument array, using the same placeholders except `{config}` and `{publisher}`.
- `publisher_inputs`: build implementation paths relative to the publisher installation, allowing an Action to supply its adapter without copies in the source repository.
- `html` and `markdown`: generated directories relative to the temporary checkout.
- `overlays`: list of `source`/`target` repository paths; `optional: true` permits an absent local input.
- `gradle_overlay`: optional `path`, exact `start`/`end` markers, `import_prefixes`, and textual `replacements`. It imports the current repository's documentation block into a historical Gradle file while preserving code outside that block.

The adapter aligns Markdown paths with HTML, including Dokka's additional module directories, and rebases cross-module Markdown links. Missing counterparts, broken Markdown links, or ambiguous destinations stop assembly.

Build fingerprints include build settings, declared repository and publisher input contents, and the source repository URL. Presentation, tag selection, API reports, and CI rules do not invalidate builds. Changing a build adapter or template must change a declared build input. The first migration from older publisher fingerprints can rebuild successful versions once; failed tags retain the explicit retry requirement.

## CI admission

Jobs can be strings, objects with `name`, or groups with `names` and a shared `fallback_steps` list. A fallback step string requires success; objects can specify `conclusions` and `optional`. Groups expand to the same independent job rules as the fully expanded form.

Admission examines the latest configured event run for the exact commit. Every configured job must exist exactly once. A successful job passes. Otherwise a job can pass through its configured `fallback_steps`: each step has a `name`, allowed `conclusions`, and optional `optional: true` to permit absence. A running job does not pass fallback checks. Job names and verification policy are configuration, not publisher code.

The Telnet config checks its aggregate gate. The SSH config checks each supported JDK's build verification and its workflow script gate, while allowing a later upload failure. Workflow-level setup and permissions remain in the calling repository's workflow.

## Version selection and reports

The site root opens the highest successfully published SemVer tag, including prereleases according to SemVer ordering, or development docs when no tagged docs exist. The dropdown reflects the viewed version. Switching preserves the current API page when available and retains only anchors found in the destination; otherwise it opens that version's homepage.

Each published version gets an HTML and Markdown API report based on its published commit. Releases compare against the preceding distinct semantic version, ignoring tag aliases and build metadata. Development docs compare against their newest tagged ancestor. Comparison tags are discovered independently of the documentation minimum, so an older undocumented tag can supply the baseline. The earliest release has a first-release report.

Reports group tracked Metalava signatures by module and declaring type. They list added, changed, and removed Kotlin APIs with links and Kotlin signatures from Dokka’s `scripts/pages.json` search index. Signatures use Dokka’s Kotlin syntax highlighting and theme colors. Overloads share their documentation page; JVM fields and Kotlin properties are grouped as one symbol. Declarations marked `@InaccessibleFromKotlin` and compiler-generated members without documentation are excluded. Removed symbols link to the baseline’s documentation when it is available; otherwise they are labeled as unavailable. If a build has no Dokka search index, symbols are listed without guessed links. They report unavailable snapshots explicitly, and distinguish new or removed modules from missing API files. This is a signature comparison; behavior changes belong in release notes.

Changelog headings can use `## [Unreleased]`, `## [0.3.12]`, `## [0.3.12][0.3.12]`, or `## v0.3.12`. Development reports use `Unreleased`; tag reports use their corresponding section. HTML renders the excerpt as CommonMark with headings, lists, links, emphasis, and code. Raw HTML is escaped, relative links resolve against the changelog at the published commit, and reference links retain their definitions. Markdown output retains the source formatting. Historical sections are included only when present in that version's source. Missing notes do not block publication.

Navigation and reports update during archive assembly, including reused versions, before sitemaps are generated. Repeated assembly replaces prior generated sections. Existing version URLs and HTML/Markdown links remain valid.

## Tests and extraction

Run these commands from the `docs/` directory of the actions repository.

```sh
python3 -m unittest discover -s scripts/tests -v
```

Tests cover the shared engine, local archive round trips, an installation outside the source repository, repository-specific CI rules, Markdown assembly, version selection, and historical API reports. The Action bundles the shared Python files, `docs.defaults.json`, and `requirements.txt`. Repository configs and build assets stay in each caller. No publisher path discovery depends on the caller's `.github/scripts` location.
