"""Explicit offline storage injection and the two-key runtime configuration contract."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check_runtime():
    from checks import _stub
    _stub.install_offline()
    from app import cli, config, llm, main, mcp, store
    from app.registry import AgentRegistry, DEFAULT_MAX_AGENTS
    from app.rag_api import status
    from rag.index import storage_root
    from rag.defaults import DEFAULT_GENERATIVE_MODEL
    from services.reminders import server as reminders
    from services.pipeline import server as pipeline

    retired = {key: '/should-not-be-used' for key in (
        'AGENT_DB_PATH', 'AGENT_MAX_LIVE', 'LLM_MAX_CONCURRENCY', 'RAG_DIR', 'RAG_MANIFEST',
        'MCP_CONFIG_PATH', 'MCP_DISABLED', 'REMIND_DB_PATH', 'REMIND_HOST', 'REMIND_PORT',
        'PIPELINE_FILES_DIR', 'OPENROUTER_SITE_URL', 'OPENROUTER_SITE_NAME')}
    with patch.dict(os.environ, retired):
        assert store.db_path() == store.DEFAULT_DB_PATH
        assert Path(store.shared_store().path).is_relative_to(Path(tempfile.gettempdir()))
        assert store.shared_store().path != store.DEFAULT_DB_PATH
        assert AgentRegistry(store=store.shared_store()).max_agents == DEFAULT_MAX_AGENTS
        assert llm.max_concurrency() == 16 and config.attribution_headers() == {}
        assert mcp.McpManager().disabled is False
        assert storage_root() == Path(__file__).resolve().parent.parent / 'data/rag'
        assert reminders.db_path() == reminders.ROOT / 'data/reminders.db'
        assert pipeline.files_dir() == pipeline.ROOT / 'files'
        assert main.NEW_CHAT_SPEC.model == cli._parse_args([]).model == DEFAULT_GENERATIVE_MODEL == 'openai/gpt-6-luna'
        with tempfile.TemporaryDirectory() as directory, patch('rag.index.storage_root', lambda: Path(directory)):
            assert status()['manifest_available'] is False
            (Path(directory) / 'inputs.json').write_text('[]')
            assert status()['manifest_available'] is True

    # A fresh process imports only config, then directs its loader to neutral .env.
    # No application registry/database, user .env or external transport is involved.
    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory)
        (fixture / '.env').write_text('OPENROUTER_API_KEY=neutral-file-key\nRAG_EMBEDDING_API_KEY=neutral-local-key\nAGENT_DB_PATH=ignored.db\nUNKNOWN_SETTING=ignored\nHTTP_PROXY=ignored-proxy\n')
        code = '''import json,os
from pathlib import Path
from unittest.mock import patch
import app.config as config
config.ROOT=Path(__import__('sys').argv[1])
original=Path.read_text
reads=[]
def read(path,*args,**kwargs):
    reads.append(str(path))
    return original(path,*args,**kwargs)
with patch.object(Path,'read_text',read):
    for _ in range(4):
        assert config.api_key()=='neutral-runtime-key'
    config._load_dotenv()
assert len(reads)==1, reads
assert os.environ['RAG_EMBEDDING_API_KEY']=='neutral-local-key'
assert os.environ['HTTP_PROXY']=='preserved-system-proxy'
assert 'UNKNOWN_SETTING' not in os.environ and 'AGENT_DB_PATH' not in os.environ
print(json.dumps({'reads':len(reads),'allowed':True}))
'''
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
        environment.update(OPENROUTER_API_KEY='neutral-runtime-key', HTTP_PROXY='preserved-system-proxy')
        result = subprocess.run([sys.executable, '-c', code, directory], cwd=Path(__file__).resolve().parent.parent,
                                env=environment, text=True, capture_output=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {'reads': 1, 'allowed': True}
    return 'retired env ignored; explicit temporary stores; fixed paths/manifest; dotenv two-key allowlist/read-once; shared default'


if __name__ == '__main__':
    print(check_runtime())
