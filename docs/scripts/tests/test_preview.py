import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('docs_preview', SCRIPTS / 'preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewEncodingTests(unittest.TestCase):
    def test_text_and_markdown_declare_utf8_without_changing_binary_types(self):
        handler = object.__new__(preview.UTF8RequestHandler)
        self.assertEqual(handler.guess_type('index.md'), 'text/markdown; charset=utf-8')
        self.assertEqual(handler.guess_type('index.html'), 'text/html; charset=utf-8')
        self.assertEqual(handler.guess_type('notes.txt'), 'text/plain; charset=utf-8')
        self.assertEqual(handler.guess_type('versions.json'), 'application/json; charset=utf-8')
        self.assertEqual(handler.guess_type('logo.png'), 'image/png')

    def test_markdown_assembly_preserves_unicode_bytes(self):
        from docs_build import assemble_publication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html, markdown = root / 'html', root / 'markdown'
            html.mkdir()
            markdown.mkdir()
            (html / 'index.html').write_text('<html></html>', encoding='utf-8')
            content = '# Terminal\n\nPTY → Terminal → Callbacks\n← ↔ — café 日本語 🚀\n'
            (markdown / 'index.md').write_text(content, encoding='utf-8')
            assemble_publication(html, markdown, root / 'site')
            self.assertEqual((root / 'site/index.md').read_bytes(), content.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
