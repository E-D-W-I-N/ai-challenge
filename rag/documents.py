"""Explicit HTML inputs → frozen normalized corpus; no discovery or crawling."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def text(self):
        return " ".join(child if isinstance(child, str) else child.text() for child in self.children)


class Tree(HTMLParser):
    # Browsers implicitly close these in legacy HTML too.
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node("root")

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "li", "td", "th", "tr"}:
            # A missing closing tag must not nest adjacent paragraphs/cells.
            ancestor = self.current
            while ancestor.parent and ancestor.tag not in {tag, "table", "ul", "ol", "body"}:
                ancestor = ancestor.parent
            if ancestor.tag == tag:
                self.current = ancestor.parent
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node
        elif tag == "br":
            self.current.children.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, text):
        self.current.children.append(text)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def decode_html(data: bytes, content_type="") -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    head = data[:8192].decode("ascii", errors="ignore")
    declared = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.I)
    header = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    encoding = declared.group(1) if declared else header.group(1) if header else None
    if encoding:
        return data.decode(encoding, errors="strict")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def normalize_html(data: bytes, source: str, content_type="") -> dict:
    tree = Tree()
    tree.feed(decode_html(data, content_type))
    title = ""
    blocks, section = [], ""
    skip_tags = {"head", "script", "style", "noscript", "nav", "footer", "form", "iframe", "svg", "object", "embed", "frameset", "noframes"}
    block_tags = {"p", "li", "dt", "dd", "blockquote", "pre", "caption"}

    def ignored(node):
        role = node.attrs.get("role", "")
        marker = node.attrs.get("class", "") + " " + node.attrs.get("id", "")
        if node.tag in skip_tags or role == "navigation" or re.search(r"\b(nav|menu|sidebar|footer|breadcrumb)\b", marker, re.I):
            return True
        # Legacy navigation cells are usually short link lists, not paragraphs.
        def descendants(item):
            for child in item.children:
                if isinstance(child, Node):
                    yield child
                    yield from descendants(child)
        links = [x for x in descendants(node) if x.tag == "a"]
        text = clean(node.text())
        return (node.tag in {"td", "div"} and len(links) >= 3 and len(text) < 1600
                and sum(len(clean(x.text())) for x in links) > len(text) * .7)

    def visible_text(node):
        if ignored(node):
            return ""
        return " ".join(x if isinstance(x, str) else visible_text(x) for x in node.children)

    def emit(text, kind):
        text = clean(text)
        if re.match(r"^(?:web[ -]?master\b|copyright\b|©|все права защищены\b)", text, re.I):
            return
        if text:
            blocks.append({"text": text, "kind": kind, "section": section})

    def walk(node):
        nonlocal title, section
        if node.tag == "title":
            title = clean(node.text())
        if ignored(node):
            # Head title is metadata, never content.
            if node.tag == "head":
                for child in node.children:
                    if isinstance(child, Node) and child.tag == "title":
                        title = clean(child.text())
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            section = clean(visible_text(node))
            emit(section, "heading")
        elif node.tag == "tr" and not any(isinstance(x, Node) and x.tag == "table" for x in node.children):
            cells = [x for x in node.children if isinstance(x, Node) and x.tag in {"td", "th"}]
            # Layout tables contain paragraphs/tables; recurse rather than flatten.
            def has_blocks(x):
                return any(isinstance(c, Node) and (c.tag in {"table", "div"} or re.fullmatch(r"h[1-6]", c.tag) or has_blocks(c)) for c in x.children)
            if cells and not any(has_blocks(c) or len(clean(visible_text(c))) > 1200 or sum(n.tag == "p" for n in descendants(c)) > 2 for c in cells):
                values = [clean(visible_text(c)) for c in cells if not ignored(c)]
                if any(values):
                    emit(" | ".join(values), "table_row")
            else:
                for child in node.children:
                    if isinstance(child, Node):
                        walk(child)
        elif node.tag == "p" and node.attrs.get("align", "").lower() == "center" and len(clean(visible_text(node))) < 250 and any(x.tag in {"b", "strong"} for x in descendants(node)):
            section = clean(visible_text(node))
            emit(section, "heading")
        elif node.tag in block_tags:
            # Nested lists retain separate item records, with the same section.
            emit(" ".join(x if isinstance(x, str) else visible_text(x) for x in node.children
                          if not isinstance(x, Node) or x.tag not in {"ul", "ol"}), node.tag)
            for child in node.children:
                if isinstance(child, Node) and child.tag in {"ul", "ol"}:
                    walk(child)
        else:
            pending = []
            for child in node.children:
                if isinstance(child, str):
                    pending.append(child)
                elif child.tag in {"b", "strong", "i", "em", "font", "span", "a", "br"}:
                    pending.append(visible_text(child))
                else:
                    emit(" ".join(pending), "paragraph")
                    pending = []
                    walk(child)
            emit(" ".join(pending), "paragraph")

    def descendants(node):
        for child in node.children:
            if isinstance(child, Node):
                yield child
                yield from descendants(child)

    nodes = list(descendants(tree.root))
    for node in nodes:
        if node.tag == "title":
            title = clean(node.text())
    # FrontPage-style layout: select the deepest wide article cell, never
    # flatten the outer table containing navigation + the nested article.
    candidates = [n for n in nodes if n.tag == "td" and n.attrs.get("width", "").isdigit()
                  and int(n.attrs["width"]) >= 500 and len(clean(visible_text(n))) >= 800
                  and sum(c.tag == "p" for c in descendants(n)) >= 3]
    leaves = [n for n in candidates if not any(c in candidates for c in descendants(n))]
    semantic = [n for n in nodes if n.tag in {"main", "article"}]
    content = max(semantic or leaves, key=lambda n: len(clean(visible_text(n)))) if semantic or leaves else tree.root
    walk(content)
    if not blocks:
        raise ValueError("HTML has no usable text")
    offset = 0
    for block in blocks:
        block["start"], block["end"] = offset, offset + len(block["text"])
        offset = block["end"] + 2
    text = "\n\n".join(block["text"] for block in blocks)
    return {"document_id": digest(source), "source": source, "title": title or source,
            "fetched_at": now(), "content_hash": digest(text), "text": text,
            "words": len(text.split()), "blocks": blocks}


def ingest(inputs: list[dict], root: Path, *, client=None) -> dict:
    """Inputs: {url} or {path, source?}; report errors without replacing corpus."""
    documents, errors, seen = [], [], set()
    with httpx.Client(timeout=30, follow_redirects=True) if client is None else _borrow(client) as http:
        for entry in inputs:
            source = "(invalid input)"
            try:
                if not isinstance(entry, dict) or not ({"url", "path"} & entry.keys()):
                    raise ValueError("Input must be a URL/path object")
                if any(not isinstance(entry[k], str) or not entry[k].strip() for k in ("url", "path", "source") if k in entry):
                    raise ValueError("URL/path/source must be nonempty strings")
                source = entry.get("source") or entry.get("url") or Path(entry["path"]).resolve().as_uri()
                if source in seen:
                    continue
                seen.add(source)
                if "path" in entry:
                    data, content_type = Path(entry["path"]).read_bytes(), ""
                else:
                    if urlparse(entry["url"]).scheme not in {"http", "https"}:
                        raise ValueError("Only explicit HTTP(S) URLs are supported")
                    response = http.get(entry["url"])
                    response.raise_for_status()
                    data, content_type = response.content, response.headers.get("content-type", "")
                    if content_type and not any(x in content_type.lower() for x in ("html", "text/", "octet-stream")):
                        raise ValueError("Input must be HTML/text, not PDF or binary")
                documents.append(normalize_html(data, source, content_type))
            except (OSError, ValueError, LookupError, httpx.HTTPError) as error:
                errors.append({"source": source, "error": str(error)})
    report = {"documents": len(documents), "words": sum(d["words"] for d in documents), "errors": errors,
              "approx_pages": sum(d["words"] for d in documents) / 500, "at": now()}
    write_json(root / "ingest-report.json", report)
    if errors or not documents:
        raise ValueError(f"Ingestion failed: {len(errors)} failed inputs; see ingest-report.json")
    fingerprint = digest(json.dumps([(d["source"], d["content_hash"]) for d in documents], separators=(",", ":")))
    write_json(root / "corpus.json", {"version": 1, "fingerprint": fingerprint, "documents": documents})
    return report


def load_corpus(root: Path) -> dict:
    corpus = json.loads((root / "corpus.json").read_text(encoding="utf-8"))
    if corpus.get("version") != 1 or not corpus.get("documents"):
        raise ValueError("Unsupported or empty corpus")
    for document in corpus["documents"]:
        if digest(document["text"]) != document["content_hash"]:
            raise ValueError("Corpus content hash mismatch")
    expected = digest(json.dumps([(d["source"], d["content_hash"]) for d in corpus["documents"]], separators=(",", ":")))
    if expected != corpus["fingerprint"]:
        raise ValueError("Corpus fingerprint mismatch")
    return corpus


class _borrow:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self.value
    def __exit__(self, *args):
        pass
