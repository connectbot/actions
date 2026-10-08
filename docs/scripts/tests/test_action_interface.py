import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from create_action_fixture import create_fixture

SCRIPTS = Path(__file__).resolve().parents[1]


class ActionInterfaceTests(unittest.TestCase):
    def test_preview_outputs_and_paths_with_spaces_without_pushing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'source with spaces'
            archive = create_fixture(repo)
            output, logs = root / 'site with spaces', root / 'logs with spaces'
            github_output = root / 'outputs'
            result = subprocess.run([sys.executable, str(SCRIPTS / 'run_action.py')], env={
                **os.environ, 'DOCS_REPOSITORY': str(repo), 'DOCS_CONFIG': '.github/docs.json',
                'DOCS_OUTPUT': str(output), 'DOCS_LOGS': str(logs), 'DOCS_RETRY_FAILED': 'false',
                'DOCS_REQUIRE_CI': 'false', 'DOCS_PUBLISH': 'false', 'GITHUB_OUTPUT': str(github_output),
            }, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            values = dict(line.split('=', 1) for line in github_output.read_text().splitlines())
            self.assertEqual(values, {'ready': 'true', 'failures': '0', 'output': str(output), 'logs': str(logs)})
            self.assertTrue((output / '0.1.0/index.html').is_file())
            self.assertTrue((output / 'main/index.md').is_file())
            refs = subprocess.check_output(['git', '-C', str(archive), 'for-each-ref'])
            self.assertEqual(refs, b'')
            self.assertFalse((repo / '.git/index').exists())

    def test_invalid_boolean_is_rejected_before_any_publication(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / 'run_action.py')],
                                env={**os.environ, 'DOCS_PUBLISH': 'yes'}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('DOCS_PUBLISH must be true or false', result.stderr)


if __name__ == '__main__':
    unittest.main()
