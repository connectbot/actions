import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('portable_publisher', SCRIPTS / 'publish-docs.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
from docs_api import changelog_excerpt, generate_reports, module_report, signatures, documentation_index, link_report, render_report, render_notes
from docs_build import prepare_checkout
from docs_config import build_fingerprint, load_config, expand_config
from docs_site import generate_site
from docs_versions import all_tags, baseline, discover, eligible, git

HTML = '<html><head></head><body><header><div id="library-version">old</div></header><div id="content"><h1>Library</h1><h2>Packages</h2></div><div class="footer"></div></body></html>'
SIGNATURE = '// Signature format: 4.0\npackage sample {\n  public class Client {\n    method public void old();\n  }\n}\n'


class PortableDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'source'
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        subprocess.run(['git', 'init', '-b', 'develop', str(self.repo)], check=True, capture_output=True)
        self.config = {
            'schema': 1, 'site': {'title': 'Example <Library>', 'base_url': 'https://docs.example.test/project'},
            'repository': {'slug': 'example/library', 'url': 'https://github.com/example/library',
                           'development_ref': 'develop', 'development_label': 'develop (snapshot)'},
            'archive': {'remote': 'archive', 'branch': 'documentation', 'author_name': 'Example bot', 'author_email': 'bot@example.test'},
            'tags': {'minimum': '0.2.0'},
            'build': {'command': ['python3', '{repo}/build.py', '{output}'], 'inputs': ['build.py'],
                      'timeout': 30, 'probe': {'path': 'module/build.gradle.kts', 'pattern': 'dokka'}},
            'api': [{'label': 'Core', 'path': 'module/api.txt', 'source_path': 'module/src/main'}],
            'changelog': 'CHANGELOG.md',
        }
        self.write('module/build.gradle.kts', 'plugins { dokka }')
        self.write('module/src/main/Client.kt', 'class Client')
        self.write('module/api.txt', SIGNATURE)
        self.write('build.py', "import sys\nfrom pathlib import Path\np=Path(sys.argv[1]); p.mkdir()\np.joinpath('index.html').write_text(" + repr(HTML) + ")\np.joinpath('index.md').write_text('# Library')\n")
        self.previous = None

    def write(self, name, value):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def commit(self, tag=None):
        tree = publisher.write_tree(self.repo, self.tree)
        args = ['-c', 'user.name=Example', '-c', 'user.email=test@example.test', 'commit-tree', tree]
        if self.previous:
            args += ['-p', self.previous]
        commit = git(self.repo, *args, data=b'fixture\n').decode().strip()
        git(self.repo, 'update-ref', 'refs/heads/develop', commit)
        self.previous = commit
        if tag:
            git(self.repo, 'tag', tag, commit)
        return commit

    def config_file(self, config=None):
        path = self.repo / 'docs.json'
        path.write_text(json.dumps(config or self.config))
        return path

    def working_inputs(self):
        shutil.copytree(self.tree, self.repo, dirs_exist_ok=True)

    def test_config_requires_schema_fields_and_relative_paths(self):
        self.working_inputs()
        load_config(self.repo, self.config_file())
        for change in ({'schema': 2}, {'site': {}}, {'changelog': '../outside'}):
            config = copy.deepcopy(self.config)
            config.update(change)
            with self.assertRaises(ValueError):
                load_config(self.repo, self.config_file(config))

    def test_config_rejects_bad_commands_and_missing_inputs(self):
        self.working_inputs()
        for key, value in [('command', 'python3 build.py'), ('command', ['{unknown}']),
                           ('timeout', -1), ('inputs', ['absent'])]:
            config = copy.deepcopy(self.config)
            config['build'][key] = value
            with self.assertRaises(ValueError):
                load_config(self.repo, self.config_file(config))

    def test_concise_dokka_config_has_shared_defaults_and_module_paths(self):
        config = expand_config({'schema': 1, 'site': self.config['site'],
                                'repository': {'slug': 'example/library'},
                                'build': {'project': 'module'}, 'api': {'module': 'Core'}})
        self.assertEqual(config['repository']['url'], 'https://github.com/example/library')
        self.assertEqual(config['repository']['development_ref'], 'main')
        self.assertEqual(config['repository']['development_label'], 'main')
        self.assertEqual(config['archive']['branch'], 'gh-pages')
        self.assertEqual(config['changelog'], 'CHANGELOG.md')
        self.assertEqual(config['build']['probe'], {'path': 'module/build.gradle.kts', 'pattern': r'\bdokka\b'})
        self.assertIn(':module:dokkaGeneratePublicationHtml', config['build']['gradle_command'])
        self.assertEqual(config['build']['html'], 'module/build/dokka/html')
        self.assertEqual(config['api'], self.config['api'])
        self.assertEqual(expand_config(config), config)

    def test_dokka_overlay_shortcuts_preserve_build_inputs_and_arguments(self):
        config = expand_config({'repository': {'slug': 'example/library'}, 'build': {
            'project': 'module', 'gradle_overlay': True,
            'arguments': ['--dependency-verification', 'strict'],
            'overlays': ['templates', {'source': 'local.properties', 'optional': True}]}})
        build = config['build']
        self.assertEqual(build['inputs'], ['module/build.gradle.kts', 'templates'])
        self.assertEqual(build['gradle_overlay']['path'], 'module/build.gradle.kts')
        self.assertEqual(build['gradle_overlay']['start'], '// BEGIN SITE DOCUMENTATION\n')
        self.assertEqual(build['gradle_command'][-2:], ['--dependency-verification', 'strict'])
        self.assertEqual(build['overlays'][0], {'source': 'templates', 'target': 'templates'})
        self.assertEqual(build['overlays'][1]['target'], 'local.properties')
        self.assertNotIn('arguments', build)
        self.assertEqual(expand_config(config), config)

    def test_defaults_allow_explicit_overrides_and_root_or_nested_projects(self):
        for project, task in [('.', ':dokkaGeneratePublicationHtml'),
                              ('libraries/core', ':libraries:core:dokkaGeneratePublicationHtml')]:
            config = expand_config({'repository': {'slug': 'example/library', 'url': 'https://git.example.test/library',
                                                   'development_ref': 'develop'},
                                    'archive': {'branch': 'documentation'}, 'changelog': None,
                                    'build': {'project': project, 'timeout': 60}})
            self.assertIn(task, config['build']['gradle_command'])
            self.assertEqual(config['repository']['url'], 'https://git.example.test/library')
            self.assertEqual(config['repository']['development_label'], 'develop')
            self.assertEqual(config['archive']['branch'], 'documentation')
            self.assertEqual(config['build']['timeout'], 60)
            self.assertIsNone(config['changelog'])
        config = expand_config({'repository': {'slug': 'example/library'},
                                'build': {'init_script': 'docs.init.gradle.kts'}})
        self.assertIn('-PdocsModule=:', config['build']['gradle_command'])
        self.assertEqual(config['build']['inputs'], ['docs.init.gradle.kts'])
        for project in ('../outside', '/outside', '', ':module'):
            with self.assertRaises(ValueError):
                expand_config({'repository': {'slug': 'example/library'}, 'build': {'project': project}})

    def test_ci_job_groups_expand_without_relaxing_admission(self):
        config = expand_config({'repository': {'slug': 'example/library'}, 'ci': {'jobs': [
            {'names': ['Build (17)', 'Build (21)'], 'fallback_steps': [
                'Verify', {'name': 'Models', 'optional': True, 'conclusions': ['success', 'skipped']}]}, 'Gate']}})
        ci = config['ci']
        self.assertEqual((ci['workflow'], ci['event'], ci['required_for_publish']), ('ci.yml', 'push', True))
        self.assertEqual([job['name'] for job in ci['jobs']], ['Build (17)', 'Build (21)', 'Gate'])
        self.assertEqual(ci['jobs'][0]['fallback_steps'], ci['jobs'][1]['fallback_steps'])
        self.assertEqual(ci['jobs'][0]['fallback_steps'][0], {'name': 'Verify', 'conclusions': ['success']})
        self.assertNotIn('fallback_steps', ci['jobs'][2])
        self.assertEqual(expand_config(config), config)
        self.working_inputs()
        self.config['ci'] = {'jobs': [{'names': ['Gate', 'Gate']}]}
        with self.assertRaises(ValueError):
            load_config(self.repo, self.config_file())

    def test_fingerprint_ignores_presentation_and_admission(self):
        self.working_inputs()
        before = build_fingerprint(self.repo, self.config)
        self.config['site']['title'] = 'New title'
        self.config['ci'] = {'workflow': 'other.yml'}
        self.config['changelog'] = 'NOTES.md'
        self.assertEqual(before, build_fingerprint(self.repo, self.config))
        (self.repo / 'build.py').write_text('changed build')
        self.assertNotEqual(before, build_fingerprint(self.repo, self.config))

    def test_discovery_and_baselines_ignore_documentation_minimum(self):
        self.commit('v0.1.0')
        second = self.commit('0.2.0')
        git(self.repo, 'tag', 'v0.2.0', second)
        tags = all_tags(self.repo)
        self.assertEqual(list(discover(self.repo, self.config)), ['develop', 'v0.2.0', '0.2.0'])
        self.assertEqual(baseline(self.repo, 'v0.2.0', second, self.config, tags), 'v0.1.0')
        self.assertEqual(baseline(self.repo, 'develop', second, self.config, tags), 'v0.2.0')
        self.assertFalse(eligible('0.2.0-rc.1', self.config))

    def test_development_baseline_uses_retained_commit(self):
        first = self.commit('0.2.0')
        self.commit('0.3.0')
        self.assertEqual(baseline(self.repo, 'develop', first, self.config, all_tags(self.repo)), '0.2.0')

    def test_reports_signature_changes_and_no_changes(self):
        first = self.commit('0.1.0')
        self.write('module/api.txt', SIGNATURE.replace('void old()', 'String old()').replace('  }', '    method public void added();\n  }'))
        second = self.commit('0.2.0')
        report = module_report(self.repo, second, first, self.config['api'][0])
        self.assertEqual(report['message'], '')
        self.assertEqual(len(report['types']), 1)
        members = {member['name']: member['change'] for member in report['types'][0]['members']}
        self.assertEqual(members, {'added': 'Added', 'old': 'Changed'})
        self.assertEqual(module_report(self.repo, second, second, self.config['api'][0])['message'], 'No public API signature changes.')

    def test_missing_snapshot_is_not_reported_as_module_removal(self):
        first = self.commit('0.1.0')
        (self.tree / 'module/api.txt').unlink()
        second = self.commit('0.2.0')
        self.assertIn('unavailable', module_report(self.repo, second, first, self.config['api'][0])['message'])

    def test_module_addition_and_removal(self):
        shutil.rmtree(self.tree / 'module')
        first = self.commit('0.1.0')
        self.write('module/src/main/Client.kt', 'class Client')
        self.write('module/api.txt', SIGNATURE)
        second = self.commit('0.2.0')
        self.assertEqual(module_report(self.repo, second, first, self.config['api'][0])['message'], 'New module.')
        shutil.rmtree(self.tree / 'module')
        third = self.commit('0.3.0')
        self.assertEqual(module_report(self.repo, third, second, self.config['api'][0])['message'], 'Removed module.')

    def test_invalid_snapshot_is_explicitly_unavailable(self):
        first = self.commit('0.1.0')
        self.write('module/api.txt', 'not signatures')
        second = self.commit('0.2.0')
        self.assertIn('unavailable', module_report(self.repo, second, first, self.config['api'][0])['message'])

    def test_signature_order_and_hex_comments_are_ignored(self):
        before = SIGNATURE.replace('    method public void old();', '    field public static final int A = 1; // 0x1\n    method public void old();')
        self.assertEqual(signatures(before), signatures(before.replace(' // 0x1', '')))

    def test_inaccessible_accessors_do_not_create_changes(self):
        before = SIGNATURE.replace('    method public void old();',
            '    method @kotlin.jvm.InaccessibleFromKotlin public String getName();\n'
            '    property public String name;')
        after = before.replace('String getName()', 'Object getName()')
        self.assertEqual(signatures(before), signatures(after))
        self.assertIn('property public String name;', signatures(before)['sample.Client'])
        self.assertNotIn('getName', str(signatures(before)))
        self.assertNotIn('getName', str(signatures(before.replace('@kotlin.jvm.', '@'))))

    def test_linked_kotlin_symbols_and_removed_baseline_links(self):
        first = self.commit('0.1.0')
        self.write('module/api.txt', SIGNATURE.replace('void old()', 'String old()').replace('  }',
            '    property public String name;\n    field public String name;\n'
            '    method public void component1();\n  }'))
        second = self.commit('0.2.0')
        output = self.site(['0.1.0', '0.2.0'])
        for version in ('0.1.0', '0.2.0'):
            directory = output / version
            (directory / 'scripts').mkdir()
            (directory / 'nested/old.html').write_text(HTML)
            (directory / 'scripts/pages.json').write_text(json.dumps([
                {'name': 'class Client', 'description': 'sample.Client', 'location': 'index.html'},
                {'name': 'fun old(): String', 'description': 'sample.Client.old', 'location': 'nested/old.html'},
                {'name': 'val name: String', 'description': 'sample.Client.name', 'location': 'nested/a space.html'},
                {'name': 'unsafe', 'description': 'sample.escape', 'location': '../../outside.html'},
            ]))
        index = documentation_index(output / '0.2.0')
        self.assertNotIn('sample.escape', index)
        report = module_report(self.repo, second, first, self.config['api'][0])
        link_report(report, index, documentation_index(output / '0.1.0'), '0.1.0')
        members = {m['name']: m for m in report['types'][0]['members']}
        self.assertEqual(set(members), {'old', 'name'})
        self.assertEqual(members['name']['documentation'][0]['href'], 'nested/a%20space.html')
        self.assertEqual(members['old']['documentation'][0]['name'], 'fun old(): String')
        removed = module_report(self.repo, first, second, self.config['api'][0])
        link_report(removed, documentation_index(output / '0.1.0'), index, '0.2.0')
        name = next(m for m in removed['types'][0]['members'] if m['name'] == 'name')
        self.assertEqual(name['change'], 'Removed')
        self.assertEqual(name['documentation'][0]['href'], '../0.2.0/nested/a%20space.html')
        html, markdown = render_report({'baseline': '0.1.0', 'comparison_url': None,
            'modules': [report], 'notes': None})
        self.assertIn('href="nested/a%20space.html"', html)
        self.assertIn('[val name: String](nested/a%20space.html)', markdown)
        self.assertNotIn('component1', html)
        self.assertNotIn('Before', html)

    def test_overloads_constructors_and_top_level_functions(self):
        before = ['public class Client', 'method @Deprecated(message="Old") public void run();']
        after = before + ['method public void run(int count);', 'ctor public Client();']
        from docs_api import declarations
        self.assertEqual(len(declarations(after)[('method', 'run')]), 2)
        report = {'message': '', 'types': [
            {'name': 'sample.Client', 'members': [
                {'kind': 'method', 'name': 'run', 'change': 'Changed'},
                {'kind': 'ctor', 'name': 'constructor', 'change': 'Added'}]},
            {'name': 'sample.HelpersKt', 'members': [
                {'kind': 'method', 'name': 'helper', 'change': 'Added'}]}]}
        index = {
            'sample.Client.run': [{'name': 'fun run()', 'href': 'run.html'},
                                  {'name': 'fun run(count: Int)', 'href': 'run.html'}],
            'sample.Client.Client': [{'name': 'constructor()', 'href': 'client.html'}],
            'sample.helper': [{'name': 'fun helper()', 'href': 'helper.html'}]}
        link_report(report, index, None, None)
        self.assertEqual(len(report['types'][0]['members'][0]['documentation']), 2)
        self.assertEqual(report['types'][0]['members'][1]['documentation'][0]['href'], 'client.html')
        self.assertEqual(report['types'][1]['members'][0]['documentation'][0]['href'], 'helper.html')

    def test_changelog_reference_headings_and_missing_sections(self):
        text = '# Changelog\n## [Unreleased]\nNext\n## [0.2.0][0.2.0]\nReleased\n## v0.1.0 - date\nOld\n[0.2.0]: https://example.test\n'
        self.assertEqual(changelog_excerpt(text, 'Unreleased'), 'Next')
        self.assertEqual(changelog_excerpt(text, 'v0.2.0'), 'Released')
        self.assertEqual(changelog_excerpt(text, '0.1.0'), 'Old')
        self.assertIsNone(changelog_excerpt(text, '0.3.0'))
        self.assertIsNone(changelog_excerpt(None, 'Unreleased'))
        self.assertFalse(changelog_excerpt('## [Unreleased]\n<!-- Add release notes -->\n', 'Unreleased'))

    def test_release_comparison_links_use_tags(self):
        self.commit('0.2.1')
        commit = self.commit('0.3.0')
        manifest = publisher.DocumentationManifest({'0.3.0': publisher.VersionDocumentation(
            published=publisher.DocumentationConfiguration(commit, 'config'))})
        report = generate_reports(self.repo, self.config, manifest, all_tags(self.repo))['0.3.0']
        self.assertEqual(report['comparison_url'], 'https://github.com/example/library/compare/0.2.1...0.3.0')
        html, markdown = render_report(report)
        self.assertIn('/compare/0.2.1...0.3.0', html)
        self.assertIn('/compare/0.2.1...0.3.0', markdown)

    def test_reports_use_published_commit_and_multiple_modules(self):
        first = self.commit('0.1.0')
        self.write('CHANGELOG.md', '## [Unreleased]\nRetained notes\n')
        second = self.commit('0.2.0')
        self.write('CHANGELOG.md', '## [Unreleased]\nNew notes\n')
        third = self.commit('0.3.0')
        self.config['api'].append({'label': 'Backend', 'path': 'backend/api.txt', 'source_path': 'backend/src/main'})
        manifest = publisher.DocumentationManifest({'develop': publisher.VersionDocumentation(
            published=publisher.DocumentationConfiguration(second, 'old'),
            attempt=publisher.BuildAttempt(publisher.DocumentationConfiguration(third, 'new'), publisher.BuildStatus.FAILED))})
        report = generate_reports(self.repo, self.config, manifest, all_tags(self.repo))['develop']
        self.assertEqual(report['baseline'], '0.2.0')
        self.assertEqual(report['notes'], 'Retained notes')
        self.assertEqual(len(report['modules']), 2)
        self.assertIn(second, report['comparison_url'])

    def site(self, names):
        output = self.root / 'site'
        output.mkdir()
        for name in names:
            path = output / name
            path.mkdir()
            (path / 'index.html').write_text(HTML)
            (path / 'index.md').write_text('# Library\n')
            (path / 'nested').mkdir()
            (path / 'nested/a space.html').write_text(HTML.replace('<h1>Library</h1>', '<h1 id="member">API</h1>'))
        return output

    def test_latest_redirect_selection_paths_and_idempotence(self):
        names = ['develop', '0.2.0', 'v0.10.0']
        output = self.site(names)
        generate_site(output, names, self.config)
        self.assertIn('https://docs.example.test/project/v0.10.0/index.html', (output / 'index.html').read_text())
        html = (output / '0.2.0/nested/a space.html').read_text()
        self.assertIn('value="0.2.0" selected', html)
        self.assertIn('develop (snapshot)', html)
        self.assertIn('/project/docs-version.js', html)
        before = {p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()}
        generate_site(output, names, self.config)
        self.assertEqual(before, {p.relative_to(output): p.read_bytes() for p in output.rglob('*') if p.is_file()})
        self.assertEqual(json.loads((output / 'versions.json').read_text())['pages']['0.2.0'], ['index.html', 'nested/a space.html'])

    def test_default_falls_back_to_development(self):
        output = self.site(['develop'])
        self.assertTrue(generate_site(output, ['develop', '0.3.0'], self.config))
        self.assertIn('/develop/index.html', (output / 'index.html').read_text())
        self.assertNotIn('0.3.0', (output / 'versions.json').read_text())

    def test_api_sections_escape_html_and_update_in_place(self):
        output = self.site(['develop'])
        report = {'baseline': '0.1.0', 'comparison_url': 'https://example.test/compare',
                  'modules': [{'label': 'Core', 'message': '', 'types': [{'name': 'Type<T>', 'members': [{'name': '<unsafe>', 'kind': 'method', 'change': 'Added'}]}]}],
                  'notes': '<script>bad()</script>', 'notes_url': 'https://example.test/notes'}
        generate_site(output, ['develop'], self.config, {'develop': report})
        html = (output / 'develop/index.html').read_text()
        self.assertIn('&lt;unsafe&gt;', html)
        self.assertNotIn('<script>bad()', html)
        report['notes'] = 'Updated'
        generate_site(output, ['develop'], self.config, {'develop': report})
        for name in ('index.html', 'index.md'):
            text = (output / 'develop' / name).read_text()
            self.assertEqual(text.count('New and changed APIs'), 1)
            self.assertNotIn('bad()', text)
            self.assertIn('Updated', text)

    def test_release_notes_render_markdown_and_resolve_source_links(self):
        notes = "### Added\n\n- Added `windowSize` and **buffering**.\n  Continued text.\n- See [guide](docs/GUIDE.md) or [release][tag].\n\n" \
                "1. Configure windows.\n2. Connect.\n\n```kotlin\nval size = 2 < 3\n```\n\n" \
                "<script>bad()</script>\n\n[unsafe](javascript:alert(1))"
        report = {'baseline': '0.1.0', 'comparison_url': None, 'modules': [],
                  'notes': notes, 'notes_url': 'https://github.com/example/library/blob/commit/CHANGELOG.md',
                  'notes_references': '[tag]: https://example.test/release'}
        html, markdown = render_report(report)
        self.assertIn('<h4>Added</h4>', html)
        self.assertIn('<ul>', html)
        self.assertIn('<ol>', html)
        self.assertIn('<code>windowSize</code>', html)
        self.assertIn('<strong>buffering</strong>', html)
        self.assertIn('<code class="language-kotlin">val size = 2 &lt; 3', html)
        self.assertIn('href="https://github.com/example/library/blob/commit/docs/GUIDE.md"', html)
        self.assertIn('href="https://example.test/release"', html)
        self.assertNotIn('<script>bad()', html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn(notes, markdown)
        self.assertIn('[tag]: https://example.test/release', markdown)

    def test_ci_missing_duplicate_and_exact_commit_rules(self):
        self.config['ci'] = {'workflow': 'verify.yml', 'event': 'push', 'jobs': [{'name': 'Gate'}]}
        for jobs in ([], [{'name': 'Gate', 'conclusion': 'failure'}], [{'name': 'Gate', 'conclusion': 'success'}] * 2):
            responses = [json.dumps({'workflow_runs': [{'id': 1, 'head_sha': 'commit'}]}).encode(), json.dumps([{'jobs': jobs}]).encode()]
            with patch.object(publisher.subprocess, 'check_output', side_effect=responses):
                self.assertFalse(publisher.ci_passed('commit', self.config))
        with patch.object(publisher.subprocess, 'check_output', return_value=b'{"workflow_runs": [{"id": 1, "head_sha": "other"}]}'):
            self.assertFalse(publisher.ci_passed('commit', self.config))

    def test_api_section_stays_inside_content_without_packages_heading(self):
        output = self.site(['develop'])
        path = output / 'develop/index.html'
        path.write_text(HTML.replace('<h2>Packages</h2>', '<div><div>Module overview</div></div>'))
        report = {'baseline': None, 'comparison_url': None, 'modules': [], 'notes': None, 'notes_url': None}
        generate_site(output, ['develop'], self.config, {'develop': report})
        text = path.read_text()
        self.assertLess(text.index('Module overview'), text.index('<section id="api-changes">'))
        self.assertLess(text.index('</section>'), text.index('</div><div class="footer">'))

    def test_gradle_overlay_replaces_only_documentation_block(self):
        self.working_inputs()
        checkout = self.root / 'checkout'
        checkout.mkdir()
        (self.repo / 'build.gradle.kts').write_text('import org.jetbrains.dokka.New\n// BEGIN DOCS\nnew config\n// END DOCS\n')
        (checkout / 'build.gradle.kts').write_text('import org.jetbrains.dokka.Old\nuntouched code\n// BEGIN DOCS\nold config\n// END DOCS\nmore code\n')
        self.config['build']['gradle_overlay'] = {'path': 'build.gradle.kts', 'start': '// BEGIN DOCS\n', 'end': '// END DOCS', 'import_prefixes': ['import org.jetbrains.dokka.']}
        prepare_checkout(self.repo, checkout, self.config)
        text = (checkout / 'build.gradle.kts').read_text()
        self.assertIn('untouched code', text)
        self.assertIn('more code', text)
        self.assertIn('new config', text)
        self.assertNotIn('old config', text)
        self.assertNotIn('dokka.Old', text)
        prepare_checkout(self.repo, checkout, self.config)
        self.assertEqual(text, (checkout / 'build.gradle.kts').read_text())

    def test_cli_runs_when_installed_outside_source_repository(self):
        self.commit('0.2.0')
        self.working_inputs()
        remote = self.root / 'archive.git'
        subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
        git(self.repo, 'remote', 'add', 'archive', str(remote))
        installed = self.root / 'installed'
        shutil.copytree(SCRIPTS, installed, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        output = self.root / 'publication'
        result = subprocess.run([sys.executable, str(installed / 'publish-docs.py'), '--repo', str(self.repo),
                                 '--config', str(self.config_file()), '--output', str(output), '--logs', str(self.root / 'logs'), '--publish'],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('/0.2.0/index.html', (output / 'index.html').read_text())
        self.assertIn('New and changed APIs', (output / '0.2.0/index.md').read_text())
        self.assertTrue(git(remote, 'rev-parse', 'refs/heads/documentation').strip())
        self.assertFalse((self.repo / '.git/index').exists())


if __name__ == '__main__':
    unittest.main()
