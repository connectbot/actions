"""Create a tiny local Git repository for composite Action smoke tests."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('fixture_publisher', SCRIPTS / 'publish-docs.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def create_fixture(repo):
    remote = repo.with_name(repo.name + '-archive.git')
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    publisher.git(repo, 'remote', 'add', 'origin', str(remote))
    with tempfile.TemporaryDirectory() as temporary:
        tree = Path(temporary)
        (tree / '.github').mkdir()
        (tree / 'module').mkdir()
        (tree / 'module/build.gradle.kts').write_text('plugins { dokka }')
        html = '<html><head></head><body><header><div id="library-version"></div></header><div id="content"><h1>Fixture</h1><h2>Packages</h2></div></body></html>'
        (tree / 'build.py').write_text('import sys\nfrom pathlib import Path\np=Path(sys.argv[1]); p.mkdir()\np.joinpath("index.html").write_text(' + repr(html) + ')\np.joinpath("index.md").write_text("# Fixture")\n')
        (tree / '.github/docs.json').write_text(json.dumps({
            'schema': 1, 'site': {'title': 'Fixture', 'base_url': 'https://docs.example.test'},
            'repository': {'slug': 'example/fixture'},
            'build': {'command': ['python3', '{checkout}/build.py', '{output}'], 'inputs': ['build.py'],
                      'probe': {'path': 'module/build.gradle.kts', 'pattern': 'dokka'}},
        }))
        oid = publisher.write_tree(repo, tree)
        commit = publisher.git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                               'commit-tree', oid, data=b'Action smoke test fixture\n').decode().strip()
        publisher.git(repo, 'update-ref', 'refs/heads/main', commit)
        publisher.git(repo, 'tag', '0.1.0', commit)
        shutil.copytree(tree, repo, dirs_exist_ok=True)
    return remote


if __name__ == '__main__':
    create_fixture(Path(sys.argv[1]))
