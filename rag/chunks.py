"""Deterministic character windows and section/paragraph packing."""
from .documents import digest

SIZE, OVERLAP = 1200, 180
STRATEGIES = ("fixed", "structural")


def windows(start, end, size=SIZE, overlap=OVERLAP):
    while start < end:
        stop = min(start + size, end)
        yield start, stop
        if stop == end:
            break
        start = stop - overlap


def chunk_documents(documents, strategy="structural", size=SIZE, overlap=OVERLAP):
    if type(size) is not int or not 64 <= size <= 100000 or type(overlap) is not int or not 0 <= overlap < size:
        raise ValueError("Chunk size must be 64–100000 and overlap between 0 and size - 1")
    if strategy not in STRATEGIES:
        raise ValueError("Unknown chunk strategy")
    chunks = []
    for document in documents:
        if strategy == "fixed":
            spans = list(windows(0, len(document["text"]), size, overlap))
        else:
            spans, start, end, section = [], None, None, None
            for block in document["blocks"]:
                a, b = block["start"], block["end"]
                if start is not None and (block["section"] != section or b - start > size):
                    spans.append((start, end))
                    start = None
                if b - a > size:
                    spans.extend(windows(a, b, size, overlap))
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
            chunk_id = digest(f'{document["document_id"]}:{strategy}:{size}:{overlap}:{start}:{end}:{content_hash}')
            chunks.append({"chunk_id": chunk_id, "document_id": document["document_id"], "source": document["source"],
                           "title": document["title"], "section": " / ".join(sections), "start": start, "end": end,
                           "content_hash": content_hash, "strategy": strategy, "text": text})
    return chunks
