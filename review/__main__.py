"""Explicit local configuration; no dotenv, target config or implicit models."""
import argparse
from pathlib import Path
import sys

from shared_models import load_connection
from shared_models.client import Models
from review.runner import publish, run


def settings(connection, chat_model, embedding_model, reasoning_enabled=False, max_tokens=4096):
    for value in (chat_model, embedding_model):
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError('Explicit valid chat and embedding model IDs are required')
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32768:
        raise ValueError('max_tokens must be 1..32768')
    return dict(connection=connection, chat_model=chat_model, embedding_model=embedding_model,
                reasoning_enabled=reasoning_enabled, max_tokens=max_tokens, temperature=0)


def main():
    parser = argparse.ArgumentParser(description='Advisory review of exact Git base/head commits; target code is never executed')
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--connection-file', required=True, type=Path)
    parser.add_argument('--chat-model', required=True)
    parser.add_argument('--embedding-model', required=True)
    parser.add_argument('--reasoning-enabled', action='store_true')
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        if (not args.connection_file.is_file() or args.connection_file.stat().st_size > 8192
                or args.connection_file.resolve().is_relative_to(args.repo.resolve()) or args.output.exists()):
            raise ValueError('Explicit bounded connection file is required')
        config = settings(load_connection(args.connection_file), args.chat_model, args.embedding_model,
                          args.reasoning_enabled, args.max_tokens)
        report = run(args.repo, args.base, args.head, Models(config))
        publish(report, args.output)
        print('Review artifacts: review.md and review.json')
        return 0
    except (ValueError, OSError):
        # Paths, raw HTTP errors and potentially echoed secrets stay out of logs.
        print('Review failed: invalid configuration, Git scope, model response or review bounds; no report published.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
