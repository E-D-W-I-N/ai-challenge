"""Trusted GitHub.com workflow adapter. PR blobs never become executable files."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import httpx

from review.__main__ import settings
from review.git import git, revision
from review.runner import publish, run
from shared_models import Connection
from shared_models.client import Models

REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')


def event_identity(event, expected_repository):
    try:
        pr = event['pull_request']
        number = event['number']
        repo = event['repository']['full_name']
        base_repo = pr['base']['repo']['full_name']
        if (type(number) is not int or not 1 <= number <= 2147483647 or not isinstance(repo, str)
                or not REPOSITORY.fullmatch(repo) or repo != expected_repository or base_repo != repo
                or pr['number'] != number or event['action'] not in {'opened', 'synchronize', 'reopened'}):
            raise ValueError()
        return repo, number, revision(pr['base']['sha']), revision(pr['head']['sha'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('Invalid pull request event identity') from None


def current(repo, number, token):
    try:
        # Fixed trusted API origin; PR URLs never control network requests or credentials.
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.get(f'https://api.github.com/repos/{repo}/pulls/{number}',
                                  headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'})
        if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
            raise ValueError()
        body = response.json()
        if body['state'] != 'open':
            raise ValueError()
        return revision(body['base']['sha']), revision(body['head']['sha'])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise ValueError('Cannot confirm current PR revisions') from None


def fetch(repo, number, base, head, token, directory):
    git(directory, 'init', '--bare', '.')
    # Credentials travel only through process environment, never argv, remote URL or config file.
    names = ('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0')
    previous = {name: os.environ.get(name) for name in names}
    try:
        import base64
        authorization = base64.b64encode(('x-access-token:' + token).encode()).decode()
        os.environ.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                          GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + authorization)
        git(directory, 'fetch', '--no-tags', '--no-recurse-submodules', '--', f'https://github.com/{repo}.git',
            f'{base}:refs/review/base', f'refs/pull/{number}/head:refs/review/head')
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if git(directory, 'rev-parse', 'refs/review/base').decode().strip() != base or git(directory, 'rev-parse', 'refs/review/head').decode().strip() != head:
        raise ValueError('Fetched PR revisions differ from event; superseded run')


def main():
    try:
        config = settings(Connection(os.environ.get('REVIEW_MODEL_URL', ''), os.environ.get('REVIEW_MODEL_API_KEY', '')),
                          os.environ.get('REVIEW_CHAT_MODEL', ''), os.environ.get('REVIEW_EMBEDDING_MODEL', ''))
        token = os.environ['REVIEW_GITHUB_TOKEN']
        if not token:
            raise ValueError('Read-only GitHub token required')
        event_file = Path(os.environ['GITHUB_EVENT_PATH'])
        if event_file.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Event exceeds size limit')
        repo, number, base, head = event_identity(json.loads(event_file.read_text()), os.environ['GITHUB_REPOSITORY'])
        if current(repo, number, token) != (base, head):
            raise ValueError('PR revisions changed; superseded run')
        with tempfile.TemporaryDirectory(prefix='review-target-', dir=os.environ['RUNNER_TEMP']) as temporary:
            target = Path(temporary)
            fetch(repo, number, base, head, token, target)
            report = run(target, base, head, Models(config))
            if current(repo, number, token) != (base, head):
                raise ValueError('PR revisions changed before report publication')
            publish(report, Path(os.environ['RUNNER_TEMP']) / 'review-artifacts')
        # Summary contains fixed text only. Full target content stays in the artifact.
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write('Advisory AI review completed. Download the review artifact for Markdown, exact revisions, sources and usage.\n')
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('Review failed: missing/invalid trusted configuration, stale PR revisions, scope bounds or model response. No new report published.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
