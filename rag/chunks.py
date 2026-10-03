"""Deterministic character windows and section/paragraph packing."""
from .documents import digest

SIZE, OVERLAP = 1200, 180
STRATEGIES = ("fixed", "structural")


def windows(start, end):
    while start < end:
        stop = min(start + SIZE, end)
        yield start, stop
        if stop == end:
            break
        start = stop - OVERLAP


def chunk_documents(documents, strategy="structural"):
    if strategy not in STRATEGIES:
        raise ValueError("Unknown chunk strategy")
    chunks = []
    for document in documents:
        if strategy == "fixed":
            spans = list(windows(0, len(document["text"])))
        else:
            spans, start, end, section = [], None, None, None
            for block in document["blocks"]:
                a, b = block["start"], block["end"]
                if start is not None and (block["section"] != section or b - start > SIZE):
                    spans.append((start, end))
                    start = None
                if b - a > SIZE:
                    spans.extend(windows(a, b))
                    continue
                if start is None:
                    start, section = a, block["section"]
                end = b
            if start is not None:
                spans.append((start, end))
        for start, end in spans:
            text = document["text"][start:end]
            sections = list(dict.fromkeys(b["section"] for b in document["blocks"]
                                         if b["start"] < end and b["end"] > start and b["section"]))
            content_hash = digest(text)
            chunk_id = digest(f'{document["document_id"]}:{strategy}:{SIZE}:{OVERLAP}:{start}:{end}:{content_hash}')
            chunks.append({"chunk_id": chunk_id, "document_id": document["document_id"], "source": document["source"],
                           "title": document["title"], "section": " / ".join(sections), "start": start, "end": end,
                           "content_hash": content_hash, "strategy": strategy, "text": text})
    return chunks
