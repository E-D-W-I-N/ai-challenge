"""In-memory RAG over one exact base revision; docs and code have separate quotas."""
import math


def chunks(path, text, revision, kind):
    # Git line numbers count LF only; Unicode separators remain source text.
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    for offset in range(0, len(lines), 60):
        body = '\n'.join(lines[offset:offset+60])
        if len(body.encode()) > 12000:
            raise ValueError('Chunk exceeds 12000 bytes; split long lines before review')
        if body.strip():
            yield dict(id=f'{revision}:{path}:{offset+1}', path=path, revision=revision,
                       kind=kind, start=offset+1, end=min(offset+60, len(lines)), text=body)


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError('Embedding dimensions differ')
    return sum(x*y for x, y in zip(a, b)) / math.sqrt(sum(x*x for x in a)*sum(y*y for y in b))


def retrieve(corpus, diffs, models, embedding_model):
    if len(corpus) > 2000:
        raise ValueError('Base corpus exceeds 2000 chunks')
    vectors = []
    for offset in range(0, len(corpus), 32):
        vectors.extend(models.embed([c['text'] for c in corpus[offset:offset+32]], embedding_model))
    queries = models.embed([d['path'] + '\n' + d['patch'] for d in diffs], embedding_model)
    selected = {}
    selections = []
    for diff, query in zip(diffs, queries):
        ids = []
        for category in ('docs', 'code'):
            ranked = sorted(((cosine(query, vector), chunk) for chunk, vector in zip(corpus, vectors)
                             if chunk['kind'] == category), key=lambda pair: (-pair[0], pair[1]['id']))
            for score, chunk in ranked[:2]:
                selected[chunk['id']] = chunk
                ids.append(dict(id=chunk['id'], score=score))
        selections.append(dict(path=diff['path'], sources=ids))
    return list(selected.values()), selections
