"""A manually launched MCP endpoint with one fixed read-only Git operation."""
import argparse
from pathlib import Path
import subprocess
from mcp.server.fastmcp import FastMCP


def git(repo, *args, allow_failure=False):
    try:
        result = subprocess.run(['git', '--no-optional-locks', *args], cwd=repo,
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Git operation unavailable') from None
    if result.returncode:
        if allow_failure: return None
        raise ValueError('Git operation failed')
    if len(result.stdout)>1024*1024: raise ValueError('Git context exceeds size limit')
    return result.stdout.rstrip('\n')


def project_context(repo):
    root = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
    return dict(project=str(root), branch=git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', allow_failure=True),
        head=git(root, 'rev-parse', '--verify', 'HEAD', allow_failure=True),
        status=git(root, 'status', '--porcelain=v1', '--untracked-files=no'))


def create_server(repository, *, host='127.0.0.1', port=8017):
    project_context(repository)
    server = FastMCP('project-git', host=host, port=port)

    @server.tool(description='Current repository identity, branch, HEAD and tracked working tree status')
    def git_project_context() -> dict:
        return project_context(repository)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8017)
    args = parser.parse_args()
    try:
        server = create_server(Path(args.repo).expanduser().resolve(), host=args.host, port=args.port)
    except ValueError as error: parser.error(str(error))
    server.run(transport='streamable-http')


if __name__ == '__main__': main()
