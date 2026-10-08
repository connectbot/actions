#!/usr/bin/env python3
"""Configurable Dokka adapter; operates only on the publisher's temporary checkout."""

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit, urlunsplit

from docs_config import repository_path


def assemble_publication(html, markdown, destination):
    """Align GFM module directories with actual HTML paths and rebase links."""
    mapping = {}
    for source in markdown.rglob("*.md"):
        relative = source.relative_to(markdown)
        parts = relative.parts
        target = relative if (html / relative.with_suffix(".html")).is_file() else (
            Path(parts[0], *parts[2:]) if len(parts) >= 3 else relative)
        if target in mapping.values():
            raise RuntimeError(f"Duplicate Markdown page: {target}")
        mapping[relative.as_posix()] = target
    for page in html.rglob("*.html"):
        if page.name != "navigation.html" and page.relative_to(html).with_suffix(".md") not in mapping.values():
            raise RuntimeError(f"HTML page has no Markdown counterpart: {page}")
    if not (html / "index.html").is_file() or not (markdown / "index.md").is_file():
        raise RuntimeError("Dokka did not produce both HTML and Markdown entrypoints")
    shutil.copytree(html, destination)
    for original, target in mapping.items():
        def rebase(match):
            url = urlsplit(match.group(1))
            if url.scheme or url.netloc or not url.path.endswith(".md"):
                return match.group(0)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(original), unquote(url.path)))
            if resolved not in mapping:
                raise RuntimeError(f"Broken Markdown link in {original}: {url.path}")
            path = os.path.relpath(mapping[resolved], target.parent).replace(os.sep, "/")
            return "](" + urlunsplit(("", "", path, url.query, url.fragment)) + ")"
        content = re.sub(r"\]\(([^)\n]+)\)", rebase, (markdown / original).read_text(encoding="utf-8"))
        path = destination / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def prepare_checkout(repo, checkout, config):
    build = config["build"]
    for overlay in build.get("overlays", []):
        source = repository_path(repo, overlay["source"])
        if not source.exists() and overlay.get("optional"):
            continue
        target = repository_path(checkout, overlay["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    overlay = build.get("gradle_overlay")
    if overlay:
        path = overlay["path"]
        current = repository_path(repo, path).read_text(encoding="utf-8")
        start, end = overlay["start"], overlay["end"]
        if current.count(start) != 1 or current.count(end) != 1:
            raise RuntimeError("Expected exactly one documentation configuration block")
        block = current.split(start, 1)[1].split(end, 1)[0]
        target = repository_path(checkout, path)
        text = target.read_text(encoding="utf-8")
        for old, new in overlay.get("replacements", {}).items():
            text = text.replace(old, new)
        if start in text:
            before, _, remainder = text.partition(start)
            _, marker, after = remainder.partition(end)
            if not marker:
                raise RuntimeError("Unterminated documentation configuration")
            text = before + after
        prefixes = overlay.get("import_prefixes", [])
        imports = [line for line in current.splitlines() if any(line.startswith(p) for p in prefixes)]
        text = "\n".join(line for line in text.splitlines() if not any(line.startswith(p) for p in prefixes)).rstrip()
        target.write_text("\n".join(imports) + "\n" + text + "\n" + start + block + end + "\n",
                          encoding="utf-8")


def run_build(repo, checkout, destination, commit, version, config):
    prepare_checkout(repo, checkout, config)
    values = {"repo": str(repo), "checkout": str(checkout), "output": str(destination),
              "commit": commit, "version": version, "repository_url": config["repository"]["url"]}
    command = [arg.format_map(values) for arg in config["build"]["gradle_command"]]
    subprocess.run(command, cwd=checkout, check=True, timeout=config["build"]["timeout"])
    assemble_publication(repository_path(checkout, config["build"]["html"]),
                         repository_path(checkout, config["build"]["markdown"]), destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "checkout", "output", "config"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("commit", "version"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    run_build(args.repo, args.checkout, args.output, args.commit, args.version, config)


if __name__ == "__main__":
    main()
