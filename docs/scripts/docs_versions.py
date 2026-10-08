"""Semantic release ordering, independent of documentation build eligibility."""

import re
import subprocess


TAG = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                 r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
                 r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")


def release_key(tag):
    match = TAG.fullmatch(tag)
    if not match:
        return None
    pre = match.group(4)
    if pre and any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in pre.split(".")):
        return None
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in pre.split(".")) if pre else ()
    return tuple(map(int, match.group(1, 2, 3))), pre is None, identifiers, tag


def eligible(tag, config):
    key = release_key(tag)
    minimum = config.get("tags", {}).get("minimum")
    return key is not None and (not minimum or key[:3] >= release_key(minimum)[:3])


def git(repo, *args, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data)


def all_tags(repo):
    return sorted((tag for tag in git(repo, "tag", "--list").decode().splitlines() if release_key(tag)),
                  key=release_key, reverse=True)


def discover(repo, config):
    development = config["repository"]["development_ref"]
    return {name: git(repo, "rev-parse", f"{ref}^{{commit}}").decode().strip()
            for name, ref in [(development, f"refs/heads/{development}")] +
            [(tag, f"refs/tags/{tag}") for tag in all_tags(repo) if eligible(tag, config)]}


def baseline(repo, name, commit, config, tags):
    development = config["repository"]["development_ref"]
    for tag in tags:
        if name != development:
            if release_key(tag)[:3] < release_key(name)[:3]:
                return tag
        else:
            result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                                     f"refs/tags/{tag}", commit], capture_output=True)
            if result.returncode == 0:
                return tag
            if result.returncode != 1:
                raise RuntimeError(result.stderr.decode())
    return None
