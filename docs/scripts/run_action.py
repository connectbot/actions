#!/usr/bin/env python3
"""Translate composite Action inputs into the portable publisher CLI."""

import os
from pathlib import Path
import subprocess
import sys


def boolean(name):
    value = os.environ.get(name, 'false')
    if value not in ('true', 'false'):
        raise ValueError(f'{name} must be true or false')
    return value == 'true'


def main():
    try:
        retry = boolean('DOCS_RETRY_FAILED')
        require_ci = boolean('DOCS_REQUIRE_CI')
        publish = boolean('DOCS_PUBLISH')
        paths = {name: os.environ[name] for name in ('DOCS_REPOSITORY', 'DOCS_CONFIG', 'DOCS_OUTPUT', 'DOCS_LOGS')}
        if any(not value or '\n' in value or '\r' in value for value in paths.values()):
            raise ValueError('Action paths must be nonempty single-line values')
    except (ValueError, KeyError) as error:
        print(f'Invalid documentation Action input: {error}', file=sys.stderr)
        return 2
    command = [sys.executable, str(Path(__file__).with_name('publish-docs.py')),
               '--repo', paths['DOCS_REPOSITORY'], '--config', paths['DOCS_CONFIG'],
               '--output', paths['DOCS_OUTPUT'], '--logs', paths['DOCS_LOGS'],
               '--retry-failed', str(retry).lower()]
    if require_ci:
        command.append('--require-ci')
    if publish:
        command.append('--publish')
    result = subprocess.run(command, check=False)
    if result.returncode == 0 and os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(f"output={Path(paths['DOCS_OUTPUT']).resolve()}\nlogs={Path(paths['DOCS_LOGS']).resolve()}\n")
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
