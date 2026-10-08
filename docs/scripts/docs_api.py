"""Public signature reports and optional release notes from published commits."""

from html import escape
import json
from pathlib import Path
import re
from urllib.parse import quote, urljoin

from markdown_it import MarkdownIt

from docs_versions import baseline, git


def read_file(repo, commit, path):
    if not git(repo, "ls-tree", "--name-only", commit, "--", path).strip():
        return None
    return git(repo, "show", f"{commit}:{path}").decode("utf-8")


def signatures(text):
    """Keep declaring type context, excluding Java-only Kotlin accessors."""
    result = {}
    package = None
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("package ") and line.endswith("{"):
            package = line.split()[1]
        elif line == "}":
            if current:
                current = None
            else:
                package = None
        elif line.endswith("{"):
            match = re.search(r"(?:class|interface|enum|@interface)\s+(\S+)", line)
            if not package or not match or current:
                raise ValueError("Unsupported API signature structure")
            current = f"{package}.{match.group(1).split('<', 1)[0]}"
            if current in result:
                raise ValueError("Duplicate declaring type")
            result[current] = [] if inaccessible(line) else [line[:-1].strip()]
        elif current and line.startswith(("ctor ", "method ", "field ", "property ", "enum_constant ")):
            if result[current] and not inaccessible(line):
                result[current].append(re.sub(r";\s*//.*$", ";", line))
        else:
            raise ValueError("Unsupported API signature declaration")
    if package or current:
        raise ValueError("Unterminated API signature scope")
    return {name: lines for name, lines in result.items() if lines}


def inaccessible(line):
    return bool(re.search(r"@(?:[\w.]+\.)?InaccessibleFromKotlin\b", line))


def declarations(lines):
    """Compare overload sets, and merge JVM fields with Kotlin properties."""
    result = {}
    for line in lines:
        kind = line.split()[0]
        if kind == "ctor":
            name = "constructor"
        elif kind == "method":
            match = re.search(r"([\w$]+)\([^()]*\)(?: throws [^;]+)?;$", line)
            if not match:
                raise ValueError("Unsupported method declaration")
            name = match.group(1)
        elif kind in ("field", "property", "enum_constant"):
            name = line.split(" =", 1)[0].rstrip(";").split()[-1]
            kind = "property"
        else:
            name, kind = "type", "type"
        result.setdefault((kind, name), set()).add(line)
    return result


def documentation_index(directory):
    """Use Dokka search entries rather than guessing generated filenames."""
    path = directory / "scripts/pages.json"
    if not path.is_file():
        return None
    result = {}
    for entry in json.loads(path.read_text(encoding="utf-8")):
        location = entry["location"]
        target = (directory / location.split("#", 1)[0]).resolve()
        if not target.is_relative_to(directory.resolve()) or not target.is_file():
            continue
        item = {"name": entry["name"], "href": quote(location, safe="/#")}
        entries = result.setdefault(entry["description"], [])
        if item not in entries:
            entries.append(item)
    return result


def link_report(report, current_docs, previous_docs, previous_version):
    for item in report["types"]:
        linked = []
        for member in item["members"]:
            removed = member["change"] == "Removed"
            index = previous_docs if removed else current_docs
            kind, name = member["kind"], member["name"]
            symbol = item["name"]
            if kind != "type":
                symbol += "." + (item["name"].rsplit(".", 1)[-1] if kind == "ctor" else name)
            docs = (index or {}).get(symbol, [])
            # Metalava represents top-level declarations in a JVM facade class.
            if not docs and item["name"].endswith("Kt") and kind != "type":
                docs = (index or {}).get(item["name"].rsplit(".", 1)[0] + "." + name, [])
            if index is not None and not docs:
                continue  # Compiler-generated members have no Kotlin documentation.
            member["documentation"] = [dict(doc, href=("../" + quote(previous_version, safe="") + "/" + doc["href"])
                                              if removed else doc["href"]) for doc in docs]
            linked.append(member)
        item["members"] = linked
        item["documentation"] = (current_docs or previous_docs or {}).get(item["name"], [])
        if item["name"] not in (current_docs or {}) and previous_docs and previous_version:
            item["documentation"] = [dict(doc, href="../" + quote(previous_version, safe="") + "/" + doc["href"])
                                     for doc in previous_docs.get(item["name"], [])]
    report["types"] = [item for item in report["types"] if item["members"]]
    if not report["types"] and not report["message"]:
        report["message"] = "No documented Kotlin API changes."


def changelog_excerpt(text, version):
    if not text:
        return None
    lines = text.splitlines()
    def excerpt(section):
        content = "\n".join(line for line in section if not re.match(r"^\[[^]]+\]:", line))
        return re.sub(r"<!--.*?-->", "", content, flags=re.S).strip()
    start = None
    for index, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        if start is not None:
            return excerpt(lines[start:index])
        match = re.match(r"##\s+\[?v?(Unreleased|\d+\.\d+\.\d+(?:[-+][\w.+-]+)?)(?=\]|\s|$)", line, re.I)
        if match and match.group(1).lower() == version.removeprefix("v").lower():
            start = index + 1
    if start is not None:
        # Reference definitions are file metadata rather than release notes.
        return excerpt(lines[start:])
    return None


def module_report(repo, commit, previous_commit, module):
    current = read_file(repo, commit, module["path"])
    previous = read_file(repo, previous_commit, module["path"]) if previous_commit else None
    current_exists = bool(git(repo, "ls-tree", "--name-only", commit, "--", module["source_path"]).strip())
    previous_exists = bool(previous_commit and git(
        repo, "ls-tree", "--name-only", previous_commit, "--", module["source_path"]).strip())
    report = {"label": module["label"], "message": "", "types": []}
    if ((current is None and current_exists) or (previous is None and previous_exists)
            or (current is None and previous is None)):
        report["message"] = "API comparison unavailable: a tracked signature snapshot is missing."
        return report
    try:
        old = {name: declarations(lines) for name, lines in signatures(previous).items()} if previous is not None else {}
        new = {name: declarations(lines) for name, lines in signatures(current).items()} if current is not None else {}
    except ValueError as error:
        report["message"] = f"API comparison unavailable: {error}."
        return report
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name, {}), new.get(name, {})
        members = []
        for kind, member in sorted(before.keys() | after.keys()):
            key = kind, member
            if before.get(key) == after.get(key):
                continue
            change = "Added" if key not in before else "Removed" if key not in after else "Changed"
            members.append({"name": member, "kind": kind, "change": change, "documentation": []})
        if members:
            report["types"].append({"name": name, "members": members, "documentation": []})
    if previous_commit and previous is None:
        report["message"] = "New module."
    elif current is None:
        report["message"] = "Removed module."
    elif not report["types"]:
        report["message"] = "No public API signature changes."
    return report


def generate_reports(repo, config, manifest, tags, output=None):
    reports = {}
    indexes = {name: documentation_index(Path(output) / name) for name, record in manifest.versions.items()
               if record.published} if output is not None else {}
    development = config["repository"]["development_ref"]
    url = config["repository"]["url"]
    for name, record in manifest.versions.items():
        if not record.published:
            continue
        commit = record.published.commit
        previous = baseline(repo, name, commit, config, tags)
        previous_commit = git(repo, "rev-parse", f"refs/tags/{previous}^{{commit}}").decode().strip() if previous else None
        target = commit if name == development else quote(name, safe="")
        reports[name] = {
            "baseline": previous,
            "comparison_url": f"{url}/compare/{quote(previous, safe='')}...{target}" if previous else None,
            "modules": [module_report(repo, commit, previous_commit, module) for module in config.get("api", [])],
            "notes": None,
            "notes_url": None,
            "notes_references": "",
        }
        for module in reports[name]["modules"]:
            link_report(module, indexes.get(name), indexes.get(previous), previous)
        if config.get("changelog"):
            text = read_file(repo, commit, config["changelog"])
            reports[name]["notes"] = changelog_excerpt(text, "Unreleased" if name == development else name)
            reports[name]["notes_references"] = "\n".join(re.findall(r"^\[[^]]+\]:[^\n]+", text or "", re.M))
            reports[name]["notes_url"] = f"{url}/blob/{commit}/{quote(config['changelog'], safe='/')}"
    return reports


def render_notes(text, source_url):
    """Render CommonMark excerpts inside the release-notes section."""
    markdown = MarkdownIt("commonmark", {"html": False})
    tokens = markdown.parse(text)
    for token in tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(6, max(4, int(token.tag[1:]) + 1))}"
        for child in token.children or []:
            attribute = "href" if child.type == "link_open" else "src" if child.type == "image" else None
            if attribute:
                child.attrSet(attribute, urljoin(source_url, child.attrGet(attribute)))
    return markdown.renderer.render(tokens, markdown.options, {})


def render_report(report):
    context = f"Changes since {report['baseline']}." if report["baseline"] else "First release; public APIs listed below."
    html = ['<section id="api-changes"><h2>New and changed APIs</h2>', f"<p>{escape(context)}</p>"]
    markdown = ["## New and changed APIs", context]
    if report["comparison_url"]:
        html.append(f'<p><a href="{escape(report["comparison_url"], quote=True)}">Compare source versions</a></p>')
        markdown.append(f'[Compare source versions]({report["comparison_url"]})')
    for module in report["modules"]:
        html.append(f'<h3>{escape(module["label"])}</h3>')
        markdown.append(f'### {module["label"]}')
        if module["message"]:
            html.append(f'<p>{escape(module["message"])}</p>')
            markdown.append(module["message"])
        for item in module["types"]:
            docs = item.get("documentation", [])
            title = f'<code class="language-kotlin">{escape(item["name"])}</code>'
            if docs:
                title = f'<a href="{escape(docs[0]["href"], quote=True)}">{title}</a>'
            html.append(f'<h4>{title}</h4><ul>')
            markdown.append(f'#### {item["name"]}')
            for member in item["members"]:
                docs = member.get("documentation", [])
                label = item["name"].rsplit(".", 1)[-1] if member["kind"] == "type" else member["name"]
                entries = docs or [{"name": label, "href": None}]
                for doc in entries:
                    label = escape(doc["name"])
                    if doc["href"]:
                        label = f'<a href="{escape(doc["href"], quote=True)}"><code class="language-kotlin">{label}</code></a>'
                        md_label = f'[{doc["name"]}]({doc["href"]})'
                    else:
                        label = f'<code class="language-kotlin">{label}</code> (documentation unavailable)'
                        md_label = f'{doc["name"]} (documentation unavailable)'
                    html.append(f'<li>{escape(member["change"])}: {label}</li>')
                    markdown.append(f'- {member["change"]}: {md_label}')
            html.append('</ul>')
    if report["notes"]:
        notes = report["notes"] + "\n\n" + report.get("notes_references", "")
        html.append('<h3>Release notes</h3><div class="docs-release-notes">'
                    + render_notes(notes, report["notes_url"]) + '</div>'
                    f'<p><a href="{escape(report["notes_url"], quote=True)}">View changelog</a></p>')
        markdown.extend(["### Release notes", notes.strip(), f'[View changelog]({report["notes_url"]})'])
    html.append("</section>")
    return "\n".join(html), "\n\n".join(markdown)
