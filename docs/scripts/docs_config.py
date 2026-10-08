"""Repository-owned configuration for the portable documentation publisher."""

import copy
import hashlib
import json
from pathlib import Path
import re
from string import Formatter
from urllib.parse import urlsplit


def repository_path(repo, value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError(f"Expected a repository-relative path: {value!r}")
    resolved = (repo / path).resolve()
    if not resolved.is_relative_to(repo.resolve()):
        raise ValueError(f"Path escapes repository: {value!r}")
    return resolved


def merge_defaults(defaults, overrides):
    """Merge objects recursively; explicit scalar and list values replace defaults."""
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_defaults(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def expand_config(raw):
    """Normalize concise repository settings into the existing publisher contract."""
    defaults = json.loads(Path(__file__).with_name("docs.defaults.json").read_text(encoding="utf-8"))
    config = merge_defaults(defaults["defaults"], raw)
    repository = config["repository"]
    for key in ("url", "development_label"):
        repository[key] = repository[key].format_map(repository)
    build = config["build"]
    if not isinstance(build["inputs"], list) or not all(isinstance(value, str) and value for value in build["inputs"]):
        raise ValueError("build.inputs must be an array of paths")
    dokka = defaults["dokka"]
    project = build.pop("project", dokka["project"])
    if not isinstance(project, str) or not project or "\\" in project:
        raise ValueError("build.project must be a relative Gradle project directory")
    project_path = Path(project)
    if project_path.is_absolute() or ".." in project_path.parts or ":" in project:
        raise ValueError("build.project must be a relative Gradle project directory")
    parts = project_path.parts
    values = {"project_dir": "/".join(parts) + "/" if parts else "",
              "project_task": ":" + ":".join(parts) + ":" if parts else ":"}
    def project_value(value):
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if "command" not in raw.get("build", {}) or build["command"] == defaults["defaults"]["build"]["command"]:
        probe = build.get("probe", {})
        if isinstance(probe, str):
            probe = {"path": probe}
        build["probe"] = merge_defaults({key: project_value(value) for key, value in dokka["probe"].items()}, probe)
        for key in ("html", "markdown"):
            build.setdefault(key, project_value(dokka[key]))
        arguments = build.pop("arguments", [])
        command = [*dokka["command"], *[project_value(task) for task in dokka["tasks"]], *dokka["arguments"], *arguments]
        script = build.pop("init_script", None)
        if script:
            command.extend(["--init-script", "{repo}/" + script, "-PdocsModule=" + (values["project_task"][:-1] or ":")])
            build["inputs"].append(script)
        build.setdefault("gradle_command", command)
    overlays = []
    for overlay in build.get("overlays", []):
        if isinstance(overlay, str):
            overlay = {"source": overlay}
        overlay = dict(overlay)
        overlay.setdefault("target", overlay["source"])
        if not overlay.get("optional"):
            build["inputs"].append(overlay["source"])
        overlays.append(overlay)
    if "overlays" in build:
        build["overlays"] = overlays
    overlay = build.get("gradle_overlay")
    if overlay:
        if overlay is True:
            overlay = {"path": project_value("{project_dir}build.gradle.kts")}
        elif isinstance(overlay, str):
            overlay = {"path": overlay}
        overlay = merge_defaults(dokka["gradle_overlay"], overlay)
        build["gradle_overlay"] = overlay
        build["inputs"].append(overlay["path"])
    build["inputs"] = sorted(set(build["inputs"]))
    api = config.get("api", [])
    if isinstance(api, dict):
        api = [{"module": module, "label": label} for module, label in api.items()]
    config["api"] = []
    for module in api:
        module = dict(module)
        directory = module.pop("module", None)
        if directory is not None:
            module = merge_defaults({key: value.format(module=directory) for key, value in defaults["api"].items()}, module)
        config["api"].append(module)
    if config.get("ci"):
        ci = merge_defaults(defaults["ci"], config["ci"])
        jobs = []
        for job in ci.get("jobs", []):
            job = {"name": job} if isinstance(job, str) else dict(job)
            steps = [merge_defaults(defaults["ci_step"], {"name": step} if isinstance(step, str) else step)
                     for step in job.get("fallback_steps", [])]
            names = job.pop("names", None)
            if names is not None:
                if not isinstance(names, list) or not names or "name" in job:
                    raise ValueError("CI job groups require a nonempty names array without name")
            else:
                names = [job["name"]]
            for name in names:
                normalized = dict(job, name=name)
                if "fallback_steps" in job:
                    normalized["fallback_steps"] = copy.deepcopy(steps)
                jobs.append(normalized)
        ci["jobs"] = jobs
        config["ci"] = ci
    return config


def load_config(repo, path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != 1:
        raise ValueError("Unsupported documentation configuration schema")
    config = expand_config(raw)
    for section, keys in {
        "site": ("title", "base_url"),
        "repository": ("slug", "url", "development_ref", "development_label"),
        "archive": ("remote", "branch", "author_name", "author_email"),
        "build": ("command", "probe", "timeout"),
    }.items():
        for key in keys:
            if not config.get(section, {}).get(key):
                raise ValueError(f"Missing documentation configuration: {section}.{key}")
    for value in (config["site"]["base_url"], config["repository"]["url"]):
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
            raise ValueError(f"Expected an HTTP URL without query or fragment: {value!r}")
    config["site"]["base_url"] = config["site"]["base_url"].rstrip("/")
    config["repository"]["url"] = config["repository"]["url"].rstrip("/")
    for value in (config["repository"]["development_ref"], config["archive"]["branch"]):
        if value.startswith("-") or any(c in value for c in " ~^:?*[\\") or ".." in value:
            raise ValueError(f"Invalid Git ref: {value!r}")
    inputs = config["build"]["inputs"]
    if not isinstance(inputs, list) or not all(isinstance(value, str) and value for value in inputs):
        raise ValueError("build.inputs must be an array of paths")
    command = config["build"]["command"]
    if not isinstance(command, list) or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("build.command must be a nonempty argument array")
    if not isinstance(config["build"]["timeout"], int) or config["build"]["timeout"] <= 0:
        raise ValueError("build.timeout must be a positive integer")
    fields = {"repo", "checkout", "output", "commit", "version", "config", "publisher", "repository_url"}
    for arg in command + config["build"].get("gradle_command", []):
        for _, field, spec, conversion in Formatter().parse(arg):
            if field is not None and (field not in fields or spec or conversion):
                raise ValueError(f"Unsupported command placeholder: {field}")
    re.compile(config["build"]["probe"]["pattern"])
    paths = [*config["build"]["inputs"], config["build"]["probe"]["path"]]
    paths.extend(config["build"][key] for key in ("html", "markdown") if key in config["build"])
    for overlay in config["build"].get("overlays", []):
        paths.extend([overlay["source"], overlay["target"]])
    if config["build"].get("gradle_overlay"):
        paths.append(config["build"]["gradle_overlay"]["path"])
    for module in config.get("api", []):
        for key in ("label", "path", "source_path"):
            if not module.get(key):
                raise ValueError(f"Missing api module {key}")
        paths.extend([module["path"], module["source_path"]])
    if config.get("changelog"):
        paths.append(config["changelog"])
    for value in paths:
        repository_path(repo, value)
    for value in config["build"]["inputs"]:
        if not repository_path(repo, value).exists():
            raise ValueError(f"Missing build input: {value}")
    publisher = Path(__file__).resolve().parent
    for value in config["build"].get("publisher_inputs", []):
        if not repository_path(publisher, value).exists():
            raise ValueError(f"Missing publisher build input: {value}")
    for arg in command:
        if "{publisher}" in arg and arg.endswith(".py"):
            adapter = Path(arg.replace("{publisher}", str(Path(__file__).resolve().parent)))
            if not adapter.is_file():
                raise ValueError(f"Missing publisher adapter: {adapter.name}")
    minimum = config.get("tags", {}).get("minimum")
    if minimum:
        from docs_versions import release_key
        if release_key(minimum) is None:
            raise ValueError("tags.minimum must be a semantic version")
    ci = config.get("ci")
    if ci:
        if not ci.get("workflow") or not ci.get("event") or not ci.get("jobs"):
            raise ValueError("ci requires workflow, event and jobs")
        names = [job["name"] for job in ci["jobs"]]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate CI job rules")
        for job in ci["jobs"]:
            for step in job.get("fallback_steps", []):
                if not step.get("name") or not step.get("conclusions"):
                    raise ValueError("CI fallback steps require names and accepted conclusions")
    return config


def build_fingerprint(repo, config):
    """Presentation and CI admission settings do not invalidate Dokka builds."""
    digest = hashlib.sha256(b"portable-docs-build-v1\0")
    digest.update(json.dumps(config["build"], sort_keys=True).encode())
    digest.update(config["repository"]["url"].encode())
    for root, inputs, prefix in (
        (repo, config["build"]["inputs"], "repo:"),
        (Path(__file__).resolve().parent, config["build"].get("publisher_inputs", []), "publisher:"),
    ):
        for value in sorted(inputs):
            path = repository_path(root, value)
            files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for file in files:
                digest.update((prefix + file.relative_to(root).as_posix()).encode() + b"\0")
                digest.update(file.read_bytes() + b"\0")
    return digest.hexdigest()
