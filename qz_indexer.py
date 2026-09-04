"""Advanced, incremental, resilient codebase indexing for Qazterion."""

from __future__ import annotations

import ast
import json
import math
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


INDEX_FILENAME = ".qazterion_index.json"
INDEX_VERSION = 4
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".cs",
    ".php", ".rb", ".c", ".cpp", ".h", ".hpp", ".sql", ".sh", ".ps1",
    ".json", ".yaml", ".yml", ".md", ".toml"
}
IGNORED_DIRECTORIES = {
    ".git", ".qazterion", "node_modules", "__pycache__", "venv", ".venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "target",
    ".next", ".nuxt", ".turbo", "vendor", "bin", "obj", ".idea", ".vscode",
    "site-packages"
}
IGNORED_SUFFIXES = {
    ".min.js", ".min.css", ".bundle.js", ".map", ".pyc", ".wasm", ".exe",
    ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".db", ".sqlite", ".sqlite3", ".db3",
    ".lock"
}
MAX_FILE_SIZE = 1_000_000
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDINGS_ENVIRONMENT_VARIABLE = "QAZTERION_ENABLE_EMBEDDINGS"
CHUNK_LINES = 120
CHUNK_OVERLAP_LINES = 20
_embedding_model: Any | None = None
_embedding_model_loaded = False

_LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".sql": "sql",
    ".sh": "shell",
    ".ps1": "powershell",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".toml": "toml",
}


def _embeddings_enabled() -> bool:
    """Return whether optional local semantic retrieval is explicitly enabled."""
    return os.getenv(EMBEDDINGS_ENVIRONMENT_VARIABLE, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_binary_file(path: Path) -> bool:
    """Fast check for binary content."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return True
        return False
    except OSError:
        return True


def _iter_source_files(workspace: Path):
    for root, directories, files in os.walk(workspace):
        directories[:] = sorted(
            directory for directory in directories
            if directory not in IGNORED_DIRECTORIES and not directory.startswith(".git")
        )
        for filename in sorted(files):
            path = Path(root, filename)
            if path.name == INDEX_FILENAME:
                continue
            suffix = path.suffix.lower()
            if suffix not in SOURCE_EXTENSIONS:
                continue
            if any(path.name.lower().endswith(ignored) for ignored in IGNORED_SUFFIXES):
                continue
            try:
                if path.stat().st_size > MAX_FILE_SIZE:
                    continue
                if _is_binary_file(path):
                    continue
                yield path
            except OSError:
                continue


def _python_symbols(source: str) -> tuple[list[str], list[str], list[str], list[str], list[str], str]:
    """Parse Python source into (functions, classes, methods, imports, symbols, summary)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], [], [], [], [], "Python source with syntax errors"
    except Exception as e:
        return [], [], [], [], [], f"Python source parsing error: {e}"

    functions: list[str] = []
    classes: list[str] = []
    methods: list[str] = []
    imports: list[str] = []
    symbols: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            symbols.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
            symbols.append(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(f"{node.name}.{item.name}")
                    symbols.append(item.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}" if module else alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.append(target.id)

    docstring = ast.get_docstring(tree) or ""
    summary_parts = []
    if classes:
        summary_parts.append(f"classes: {', '.join(classes[:8])}")
    if functions:
        summary_parts.append(f"functions: {', '.join(functions[:8])}")
    if imports:
        summary_parts.append(f"imports: {', '.join(imports[:6])}")

    summary = docstring.splitlines()[0][:200] if docstring else "; ".join(summary_parts)
    return functions, classes, methods, imports, sorted(set(symbols)), summary or "Python source"


def _generic_symbols(source: str, suffix: str) -> tuple[list[str], list[str], list[str], list[str], list[str], str]:
    """Extract functions, classes, methods, imports, symbols, summary for non-Python files."""
    functions: list[str] = []
    classes: list[str] = []
    methods: list[str] = []
    imports: list[str] = []
    symbols: list[str] = []
    summary = ""

    lines = source.splitlines()
    for line in lines[:20]:
        sline = line.strip()
        if not summary and sline and (sline.startswith("//") or sline.startswith("#") or sline.startswith("/*")):
            summary = sline.lstrip("/#* ")[:200]

    # JS/TS
    if suffix in (".js", ".jsx", ".ts", ".tsx"):
        for m in re.finditer(r"\bclass\s+([A-Za-z0-9_$]+)", source):
            classes.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\b(?:interface|type)\s+([A-Za-z0-9_$]+)", source):
            symbols.append(m.group(1))
        for m in re.finditer(r"\b(?:function\s+([A-Za-z0-9_$]+)|const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)", source):
            name = m.group(1) or m.group(2)
            if name:
                functions.append(name)
                symbols.append(name)
        for m in re.finditer(r"(?:import\s+(?:\{[^}]+\}|[A-Za-z0-9_$]+|\*\s+as\s+[A-Za-z0-9_$]+)\s+from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", source):
            imp = m.group(1) or m.group(2)
            if imp:
                imports.append(imp)

    # Go
    elif suffix == ".go":
        for m in re.finditer(r"\bfunc\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\(", source):
            functions.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\btype\s+([A-Za-z0-9_]+)\s+struct\b", source):
            classes.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\bimport\s+(?:\(\s*([^)]+)\s*\)|['\"]([^'\"]+)['\"])", source):
            block, single = m.groups()
            if single:
                imports.append(single)
            elif block:
                for line in block.splitlines():
                    imp = line.strip().strip('"')
                    if imp:
                        imports.append(imp)

    # Rust
    elif suffix == ".rs":
        for m in re.finditer(r"\bfn\s+([A-Za-z0-9_]+)\s*\(", source):
            functions.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\b(?:struct|enum|trait)\s+([A-Za-z0-9_]+)", source):
            classes.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\buse\s+([^;]+);", source):
            imports.append(m.group(1).strip())

    # Java / C#
    elif suffix in (".java", ".cs"):
        for m in re.finditer(r"\b(?:class|interface|record)\s+([A-Za-z0-9_]+)", source):
            classes.append(m.group(1))
            symbols.append(m.group(1))
        for m in re.finditer(r"\b(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*\{", source):
            name = m.group(1)
            if name not in ("if", "for", "while", "switch", "catch"):
                functions.append(name)
                symbols.append(name)
        for m in re.finditer(r"\b(?:import|using)\s+([^;]+);", source):
            imports.append(m.group(1).strip())

    if not summary:
        parts = []
        if classes:
            parts.append(f"classes: {', '.join(classes[:6])}")
        if functions:
            parts.append(f"functions: {', '.join(functions[:6])}")
        summary = "; ".join(parts) if parts else "Source file"

    return functions, classes, methods, imports, sorted(set(symbols)), summary


def _embedding_text(entry: dict[str, Any]) -> str:
    """Return the compact text representation used for local semantic search."""
    return " ".join(
        part for part in (
            entry.get("path", "").replace("/", " "),
            entry.get("summary", ""),
            " ".join(entry.get("functions", [])),
            " ".join(entry.get("classes", [])),
            " ".join(entry.get("methods", [])),
            " ".join(entry.get("imports", [])),
            " ".join(entry.get("symbols", [])),
        ) if part
    )


def _get_embedding_model():
    """Load the local embedding model once, returning ``None`` when unavailable."""
    global _embedding_model, _embedding_model_loaded
    if not _embeddings_enabled():
        return None
    if _embedding_model_loaded:
        return _embedding_model

    _embedding_model_loaded = True
    try:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME, local_files_only=True)
    except Exception:
        _embedding_model = None
    return _embedding_model


def _normalized_embeddings(model: Any, texts: list[str]) -> list[list[float]]:
    """Encode *texts* as JSON-safe, unit-length vectors."""
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [[float(value) for value in vector] for vector in vectors]


def _build_chunks(path: str, source: str, functions: list[str], classes: list[str], methods: list[str] | None = None) -> list[dict[str, Any]]:
    """Build compact, line-addressable search units without caching full source code."""
    lines = source.splitlines()
    if not lines:
        return []

    chunks = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP_LINES)
    symbols = functions + classes + (methods or [])
    for start in range(0, len(lines), step):
        end = min(len(lines), start + CHUNK_LINES)
        text = "\n".join(lines[start:end])
        chunk_symbols = [symbol for symbol in symbols if re.search(rf"\b{re.escape(symbol.split('.')[-1])}\b", text)]
        keywords = sorted(set(_TOKEN_RE.findall(text.lower())))
        chunks.append({
            "path": path,
            "start_line": start + 1,
            "end_line": end,
            "symbols": chunk_symbols,
            "keywords": keywords,
            "_embedding_text": f"{path} {' '.join(chunk_symbols)}\n{text[:6000]}",
        })
        if end == len(lines):
            break
    return chunks


def _resolve_dependencies(files: list[dict[str, Any]]) -> None:
    """Link imports across indexed files into relative workspace dependency paths."""
    path_map: dict[str, str] = {}
    for entry in files:
        p = entry["path"]
        path_map[p] = p
        no_ext = os.path.splitext(p)[0]
        path_map[no_ext] = p
        path_map[no_ext.replace("/", ".")] = p
        path_map[os.path.basename(no_ext)] = p

    for entry in files:
        deps = set()
        for imp in entry.get("imports", []):
            imp_clean = imp.split()[0].replace("\\", "/").strip("'\"")
            if imp_clean.startswith("."):
                dir_path = os.path.dirname(entry["path"])
                norm = os.path.normpath(os.path.join(dir_path, imp_clean)).replace("\\", "/")
                if norm in path_map:
                    deps.add(path_map[norm])
                elif f"{norm}.py" in path_map:
                    deps.add(path_map[f"{norm}.py"])
                elif f"{norm}.ts" in path_map:
                    deps.add(path_map[f"{norm}.ts"])
                elif f"{norm}.js" in path_map:
                    deps.add(path_map[f"{norm}.js"])

            parts = imp_clean.split(".")
            for i in range(len(parts), 0, -1):
                candidate = ".".join(parts[:i])
                if candidate in path_map:
                    target = path_map[candidate]
                    if target != entry["path"]:
                        deps.add(target)
                        break

        entry["dependencies"] = sorted(deps)


def _index_single_file(workspace: Path, path: Path) -> dict[str, Any]:
    """Parse and index a single source file safely."""
    rel_path = path.relative_to(workspace).as_posix()
    stat = path.stat()
    suffix = path.suffix.lower()
    language = _LANGUAGE_MAP.get(suffix, "text")

    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        source = ""

    if suffix == ".py":
        functions, classes, methods, imports, symbols, summary = _python_symbols(source)
    else:
        functions, classes, methods, imports, symbols, summary = _generic_symbols(source, suffix)

    chunks = _build_chunks(rel_path, source, functions, classes, methods)

    return {
        "path": rel_path,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "language": language,
        "functions": functions,
        "classes": classes,
        "methods": methods,
        "imports": imports,
        "symbols": symbols,
        "dependencies": [],
        "summary": summary or "Source file",
        "chunks": chunks,
    }


def build_index(workspace_path: str | os.PathLike[str], existing_index: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create and persist an incremental source-file index for *workspace_path*."""
    workspace = Path(workspace_path).resolve()
    current_files = list(_iter_source_files(workspace))

    cached_map: dict[str, dict[str, Any]] = {}
    if existing_index and isinstance(existing_index.get("files"), list):
        for entry in existing_index["files"]:
            if isinstance(entry, dict) and "path" in entry:
                cached_map[entry["path"]] = entry

    files: list[dict[str, Any]] = []
    new_or_modified_entries: list[dict[str, Any]] = []

    for path in current_files:
        rel_path = path.relative_to(workspace).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue

        cached = cached_map.get(rel_path)
        if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
            files.append(cached)
        else:
            try:
                entry = _index_single_file(workspace, path)
                files.append(entry)
                new_or_modified_entries.append(entry)
            except Exception as e:
                # Keep index resilient if one file cannot be parsed
                entry = {
                    "path": rel_path,
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "language": _LANGUAGE_MAP.get(path.suffix.lower(), "text"),
                    "functions": [],
                    "classes": [],
                    "methods": [],
                    "imports": [],
                    "symbols": [],
                    "dependencies": [],
                    "summary": f"Unparsed file ({e})",
                    "chunks": [],
                }
                files.append(entry)

    # Resolve dependencies across all indexed files
    _resolve_dependencies(files)

    # Embed new/modified entries if embedding model is available
    model = _get_embedding_model()
    if model is not None and new_or_modified_entries:
        try:
            vectors = _normalized_embeddings(model, [_embedding_text(entry) for entry in new_or_modified_entries])
            for entry, vector in zip(new_or_modified_entries, vectors):
                entry["embedding"] = vector

            chunks = [chunk for entry in new_or_modified_entries for chunk in entry.get("chunks", []) if "_embedding_text" in chunk]
            if chunks:
                chunk_vectors = _normalized_embeddings(model, [chunk["_embedding_text"] for chunk in chunks])
                for chunk, vector in zip(chunks, chunk_vectors):
                    chunk["embedding"] = vector
        except Exception:
            pass

    for entry in files:
        for chunk in entry.get("chunks", []):
            chunk.pop("_embedding_text", None)

    index = {
        "version": INDEX_VERSION,
        "workspace": str(workspace),
        "embedding_model": EMBEDDING_MODEL_NAME if any("embedding" in entry for entry in files) else None,
        "files": files,
    }

    cache_path = workspace / INDEX_FILENAME
    temp_cache_path = workspace / f"{INDEX_FILENAME}.tmp"
    try:
        temp_cache_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        if temp_cache_path.exists():
            temp_cache_path.replace(cache_path)
    except OSError:
        try:
            cache_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    return index


def _cache_is_current(index: dict[str, Any], workspace: Path) -> bool:
    if index.get("version") not in (3, INDEX_VERSION) or index.get("workspace") != str(workspace):
        return False

    cached_files = {entry.get("path"): entry for entry in index.get("files", []) if isinstance(entry, dict)}
    current_paths = list(_iter_source_files(workspace))
    current_files = {path.relative_to(workspace).as_posix(): path for path in current_paths}

    if set(cached_files) != set(current_files):
        return False

    for relative_path, path in current_files.items():
        try:
            stat = path.stat()
        except OSError:
            return False
        entry = cached_files[relative_path]
        if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
            return False
    return True


def load_or_build_index(workspace_path: str | os.PathLike[str]) -> tuple[dict[str, Any], bool]:
    """Load a current cache, or incrementally rebuild it. Returns ``(index, was_rebuilt)``."""
    workspace = Path(workspace_path).resolve()
    cache_path = workspace / INDEX_FILENAME
    existing_index = None
    try:
        if cache_path.is_file():
            existing_index = json.loads(cache_path.read_text(encoding="utf-8"))
            if _cache_is_current(existing_index, workspace):
                return existing_index, False
    except (OSError, json.JSONDecodeError):
        existing_index = None

    return build_index(workspace, existing_index=existing_index), True


def _doc_tokens(entry: dict[str, Any]) -> set[str]:
    text = " ".join([
        entry.get("path", ""),
        entry.get("summary", ""),
        " ".join(entry.get("functions", [])),
        " ".join(entry.get("classes", [])),
        " ".join(entry.get("methods", [])),
        " ".join(entry.get("symbols", [])),
        " ".join(entry.get("imports", [])),
    ])
    return set(_TOKEN_RE.findall(text.lower()))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """Return cosine similarity without depending on NumPy."""
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _query_embedding(index: dict[str, Any], query: str) -> list[float] | None:
    """Embed a query only when this index contains compatible file vectors."""
    if index.get("embedding_model") != EMBEDDING_MODEL_NAME:
        return None
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        return _normalized_embeddings(model, [query])[0]
    except Exception:
        return None


def _symbol_tokens(symbols_str: str) -> set[str]:
    """Tokenize symbols splitting both underscores and camelCase."""
    tokens = set(_TOKEN_RE.findall(symbols_str.lower()))
    for t in list(tokens):
        # camelCase split
        camel_parts = re.findall(r"[a-z]+|[A-Z][a-z]*|\d+", t)
        for part in camel_parts:
            if len(part) >= 2:
                tokens.add(part.lower())
    return tokens


def search_index(index: dict[str, Any], query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return relevant files using cached local embeddings, exact/symbol matches, and lexical fallback."""
    tokens = {token.lower() for token in _TOKEN_RE.findall(query)}
    if not tokens:
        return []

    files = index.get("files", [])
    if not files:
        return []

    n_docs = len(files)
    doc_token_sets = [_doc_tokens(entry) for entry in files]
    query_vector = _query_embedding(index, query)
    doc_freq = {t: sum(1 for dts in doc_token_sets if t in dts) for t in tokens}

    scored = []
    for entry, doc_token_set in zip(files, doc_token_sets):
        path_l = entry["path"].lower()
        symbols_str = " ".join(
            entry.get("functions", []) + entry.get("classes", []) +
            entry.get("methods", []) + entry.get("symbols", [])
        ).lower()
        sym_tokens = _symbol_tokens(symbols_str)
        imports = " ".join(entry.get("imports", [])).lower()
        summary = entry.get("summary", "").lower()

        lexical_score = 0.0
        for t in tokens:
            field_weight = 0
            if t in path_l:
                field_weight = 6
            elif t in sym_tokens:
                field_weight = 5
            elif any(s.startswith(t[:5]) or t.startswith(s[:5]) for s in sym_tokens if len(t) >= 5 and len(s) >= 5):
                field_weight = 4
            elif t in imports:
                field_weight = 2
            elif t in summary:
                field_weight = 1

            if field_weight:
                idf = math.log((n_docs + 1) / (doc_freq.get(t, 0) + 1)) + 1
                lexical_score += field_weight * idf

        if lexical_score == 0.0:
            best_ratio = 0.0
            for t in tokens:
                for dt in doc_token_set:
                    ratio = SequenceMatcher(None, t, dt).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
            if best_ratio >= 0.8:
                lexical_score = best_ratio

        semantic_score = 0.0
        if query_vector is not None and isinstance(entry.get("embedding"), list):
            semantic_score = max(0.0, _cosine_similarity(query_vector, entry["embedding"]))

        score = semantic_score * 10 + lexical_score
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda item: (-item[0], item[1]["path"]))
    return [entry for _, entry in scored[:max(1, limit)]]


def search_chunks(index: dict[str, Any], query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return the most relevant line ranges across already-indexed source files."""
    tokens = set(_TOKEN_RE.findall(query.lower()))
    if not tokens:
        return []

    chunks = [chunk for entry in index.get("files", []) for chunk in entry.get("chunks", [])]
    if not chunks:
        return []

    query_vector = _query_embedding(index, query)
    document_tokens = [set(chunk.get("keywords", [])) for chunk in chunks]
    document_frequency = {token: sum(token in values for values in document_tokens) for token in tokens}
    candidates = []
    for chunk, keywords in zip(chunks, document_tokens):
        path = chunk["path"].lower()
        symbols_str = " ".join(chunk.get("symbols", [])).lower()
        sym_tokens = _symbol_tokens(symbols_str)
        lexical_score = 0.0
        for token in tokens:
            weight = 0
            if token in path:
                weight = 5
            elif token in sym_tokens:
                weight = 4
            elif any(s.startswith(token[:5]) or token.startswith(s[:5]) for s in sym_tokens if len(token) >= 5 and len(s) >= 5):
                weight = 3
            elif token in keywords:
                weight = 1
            elif any(k.startswith(token[:5]) or token.startswith(k[:5]) for k in keywords if len(token) >= 5 and len(k) >= 5):
                weight = 1

            if weight:
                idf = math.log((len(chunks) + 1) / (document_frequency.get(token, 0) + 1)) + 1
                lexical_score += weight * idf

        semantic_score = 0.0
        if query_vector is not None and isinstance(chunk.get("embedding"), list):
            semantic_score = max(0.0, _cosine_similarity(query_vector, chunk["embedding"]))
        if semantic_score or lexical_score:
            candidates.append((lexical_score, semantic_score, chunk))

    use_lexical = any(lexical_score > 0 for lexical_score, _, _ in candidates)
    scored = [
        ((lexical_score * 10 + semantic_score) if use_lexical else semantic_score, chunk)
        for lexical_score, semantic_score, chunk in candidates
    ]

    scored.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start_line"]))
    return [chunk for _, chunk in scored[:max(1, limit)]]


def format_search_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No relevant indexed source files found. Try a different query or use list_files."
    lines = []
    for entry in results:
        symbols = entry.get("functions", []) + entry.get("classes", [])
        detail = f"; symbols: {', '.join(symbols)}" if symbols else ""
        lines.append(f"{entry['path']} — {entry.get('summary', 'Source file')}{detail}")
    return "\n".join(lines)


def format_index_summary(index: dict[str, Any], limit: int = 80) -> str:
    entries = index.get("files", [])
    lines = [f"Indexed source files: {len(entries)}"]
    for entry in entries[:limit]:
        symbols = entry.get("functions", []) + entry.get("classes", [])
        suffix = f" ({', '.join(symbols)})" if symbols else ""
        lines.append(f"- {entry['path']}{suffix}")
    if len(entries) > limit:
        lines.append(f"- ... and {len(entries) - limit} more; use search_index for a focused lookup.")
    return "\n".join(lines)
