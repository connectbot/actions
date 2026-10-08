"""Assemble navigation, API sections and sitemaps for a documentation archive."""

from html import escape
from html.parser import HTMLParser
import json
import re
from urllib.parse import quote
import xml.etree.ElementTree as ET

from docs_api import render_report
from docs_versions import release_key


VERSION_SCRIPT = r'''"use strict";
(async () => {
    const script = document.querySelector("script[data-docs-root]");
    const select = document.getElementById("docs-version");
    if (!script || !select) return;
    const root = new URL(script.dataset.docsRoot, location.href);
    const current = script.dataset.docsVersion;
    select.addEventListener("change", async () => {
        const chosen = select.value;
        select.disabled = true;
        const versionRoot = new URL(encodeURIComponent(chosen) + "/", root);
        let target = new URL("index.html", versionRoot);
        try {
            const response = await fetch(new URL("versions.json", root));
            if (!response.ok) throw new Error("Version index unavailable");
            const versions = await response.json();
            const prefix = new URL(encodeURIComponent(current) + "/", root).pathname;
            const relative = decodeURIComponent(location.pathname.slice(prefix.length)) || "index.html";
            if (location.pathname.startsWith(prefix) && versions.pages[chosen].includes(relative)) {
                target = new URL(relative.split("/").map(encodeURIComponent).join("/"), versionRoot);
                if (location.hash) {
                    const page = await fetch(target);
                    if (page.ok) {
                        const doc = new DOMParser().parseFromString(await page.text(), "text/html");
                        const anchor = decodeURIComponent(location.hash.slice(1));
                        if (doc.getElementById(anchor) || Array.from(doc.getElementsByName(anchor)).length) {
                            target.hash = location.hash;
                        }
                    }
                }
            }
        } catch (_) {
            // The selected version's homepage remains usable without the index.
        }
        location.assign(target.href);
    });
})();
'''

VERSION_STYLE = '''#library-version { display: flex; align-items: center; padding-top: 0; margin-inline-start: 16px; }
.docs-version-label { display: inline-flex; align-items: center; gap: 8px; line-height: 24px;
    padding: 4px 10px; border: 1px solid rgba(255, 255, 255, .2); border-radius: 6px;
    background: rgba(255, 255, 255, .035); white-space: nowrap; max-width: 100%; box-sizing: border-box; }
.docs-version-label:focus-within { border-color: rgba(255, 255, 255, .5); }
#docs-version { font: inherit; line-height: 24px; color: inherit; background: transparent; border: 0;
    border-radius: 4px; padding: 2px 4px; min-width: 0; max-width: 15rem; cursor: pointer; }
#docs-version:focus-visible { outline: 2px solid #307fff; outline-offset: 2px; }
#docs-version option { color: #222; background: #fff; }
#api-changes { margin: 1.5rem 0; overflow-wrap: anywhere; }
#api-changes code.language-kotlin, #api-changes code.language-kotlin .token { white-space: pre-wrap; overflow-wrap: anywhere; }
#api-changes pre { white-space: pre-wrap; overflow-wrap: anywhere; overflow-x: auto; }
@media (max-width: 899px) {
    #library-version { margin-inline-start: 0; padding-inline: 12px; flex-basis: 0; min-width: 0; }
}
@media (max-width: 600px) {
    .docs-version-label { font-size: 14px; padding: 4px 6px; gap: 6px; }
    #docs-version { max-width: 9rem; padding-inline: 2px; }
}
'''


def replace_block(text, name, replacement):
    pattern = rf"<!-- BEGIN {name} -->.*?<!-- END {name} -->\s*"
    return re.sub(pattern, "", text, flags=re.S), f"<!-- BEGIN {name} -->\n{replacement}\n<!-- END {name} -->\n"


def content_end(text):
    """Locate the content container's closing tag, including nested divs."""
    class ContentParser(HTMLParser):
        depth = 0
        end = None

        def handle_starttag(self, tag, attrs):
            if tag == "div":
                if self.depth:
                    self.depth += 1
                elif self.end is None and dict(attrs).get("id") == "content":
                    self.depth = 1

        def handle_endtag(self, tag):
            if tag == "div" and self.depth:
                self.depth -= 1
                if not self.depth:
                    line, column = self.getpos()
                    self.end = sum(len(line_text) for line_text in text.splitlines(keepends=True)[:line - 1]) + column

    parser = ContentParser()
    parser.feed(text)
    return parser.end


def augment_page(text, name, available, config, report=None):
    base = config["site"]["base_url"]
    development = config["repository"]["development_ref"]
    options = []
    for version in available:
        label = config["repository"]["development_label"] if version == development else version
        selected = ' selected' if version == name else ''
        options.append(f'<option value="{escape(version, quote=True)}"{selected}>{escape(label)}</option>')
    selector = '<div class="library-version" id="library-version"><label class="docs-version-label">' \
               'Version <select id="docs-version" aria-label="Documentation version">' + ''.join(options) + '</select></label></div>'
    text = re.sub(r'<div\b[^>]*\bid="library-version"[^>]*>.*?</div>', lambda _: selector,
                  text, count=1, flags=re.S)
    if 'id="docs-version"' not in text:
        text = text.replace('</header>', selector + '\n</header>', 1)
    text, assets = replace_block(text, "DOCS NAVIGATION",
        f'<link rel="stylesheet" href="{escape(base, quote=True)}/docs-version.css">\n'
        f'<script src="{escape(base, quote=True)}/docs-version.js" defer '
        f'data-docs-root="{escape(base, quote=True)}/" data-docs-version="{escape(name, quote=True)}"></script>')
    text = text.replace('</head>', assets + '</head>', 1)
    text, section = replace_block(text, "DOCS API CHANGES", render_report(report)[0] if report else '')
    if report:
        match = re.search(r'<h2\b[^>]*>\s*(?:Packages|Modules)\s*</h2>', text)
        if match:
            text = text[:match.start()] + section + text[match.start():]
        else:
            position = content_end(text)
            if position is None:
                raise RuntimeError("Cannot locate documentation homepage content")
            text = text[:position] + section + text[position:]
    return text


def generate_site(output, versions, config, reports=None):
    development = config["repository"]["development_ref"]
    base = config["site"]["base_url"]
    available = [name for name in versions if (output / name / "index.html").is_file()]
    releases = sorted((name for name in available if name != development), key=release_key, reverse=True)
    ordered = releases + ([development] if development in available else [])
    pages = {}
    for name in ordered:
        pages[name] = [page.relative_to(output / name).as_posix()
                       for page in sorted((output / name).rglob("*.html")) if page.name != "navigation.html"]
        for relative in pages[name]:
            page = output / name / relative
            report = (reports or {}).get(name) if relative == "index.html" else None
            page.write_text(augment_page(page.read_text(encoding="utf-8"), name, ordered, config, report), encoding="utf-8")
        report = (reports or {}).get(name)
        if report:
            path = output / name / "index.md"
            text, section = replace_block(path.read_text(encoding="utf-8"), "DOCS API CHANGES", render_report(report)[1])
            path.write_text(text.rstrip() + '\n\n' + section, encoding="utf-8")
    (output / "versions.json").write_text(json.dumps({"versions": ordered, "pages": pages}), encoding="utf-8")
    (output / "docs-version.js").write_text(VERSION_SCRIPT, encoding="utf-8")
    (output / "docs-version.css").write_text(VERSION_STYLE, encoding="utf-8")
    sitemap_index = ET.Element("sitemapindex", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for name in ordered:
        sitemap = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for relative in pages[name]:
            entry = ET.SubElement(sitemap, "url")
            ET.SubElement(entry, "loc").text = base + "/" + quote(name + "/" + relative, safe="/")
        ET.ElementTree(sitemap).write(output / name / "sitemap.xml", encoding="utf-8", xml_declaration=True)
        entry = ET.SubElement(sitemap_index, "sitemap")
        ET.SubElement(entry, "loc").text = f"{base}/{quote(name, safe='')}/sitemap.xml"
    if development not in available:
        return False
    target = f"{base}/{quote(ordered[0], safe='')}/index.html"
    title = escape(config["site"]["title"])
    href = escape(target, quote=True)
    index = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Documentation</title><meta http-equiv="refresh" content="0;url={href}">
<link rel="canonical" href="{href}"></head><body><p><a href="{href}">Open {title} documentation ({escape(ordered[0])})</a></p></body></html>
'''
    (output / "index.html").write_text(index, encoding="utf-8")
    ET.ElementTree(sitemap_index).write(output / "sitemap_index.xml", encoding="utf-8", xml_declaration=True)
    return True
