"""Tool sets for the C5 token-efficiency eval — the three arms' tool surfaces.

Each arm hands the SAME agent the SAME task with a DIFFERENT tool set; the
loop in `_agent_loop.py` measures what the agent spends. The arms mirror
codegraph's MCP-on / MCP-off comparison:

  - baseline : `list_files` / `read_file` / `grep` — the honest status quo, an
               agent navigating a repo with built-in Read/Grep. read_file
               supports a line range so the baseline is not strawmanned.
  - rag      : `search_chunks` (top-k lexical chunks) + `read_file` — what a
               vanilla embedding/RAG store gives an agent.
  - anfs     : `outline` / `symbol_search` / `callers` / `context_pack` /
               `read_span`, plus `grep`/`read_file` as fallback (MCP-on keeps
               Read/Grep too). `context_pack` records a `code_context_query`
               audit event, so the same call that saves tokens is replayable.

All addressing is by repo-relative path; node ids stay inside `Corpus`.
"""

import re

from _agent_loop import Tool

# Tool result strings are capped so one call can't blow the context window and
# distort the token measurement; truncation is announced, never silent.
_MAX_GREP_HITS = 60
_MAX_LIST = 400
_DEFAULT_PACK_BUDGET = 1500
_CHUNK_LINES = 40


class Corpus:
    """An indexed repo: path<->node_id maps, fragments indexed, symbols cached.

    Build once with `Corpus.index(...)`, then pass to the toolset factories.
    """

    def __init__(self, fs, files):
        self.fs = fs
        self.files = dict(files)  # path -> node_id
        self._node_to_path = {nid: path for path, nid in self.files.items()}
        self._by_name = None  # name -> [(path, fragment_id, kind, start, end)]
        self._pack_calls = 0

    @classmethod
    def index(cls, fs, sources, workspace="ws:corpus", agent_id="corpus_agent",
              parser="tree-sitter-rust"):
        """Write `sources` ({path: text}) into ANFS and index their fragments."""
        ws = fs.open_workspace(workspace, agent_id)
        files = {}
        for path, text in sources.items():
            nid = ws.write(path, text.encode(), [])
            fs.index_node_fragments(nid, parser)
            files[path] = nid
        return cls(fs, files)

    # --- internal helpers -------------------------------------------------
    def read_text(self, node_id):
        return bytes(self.fs.read_node(node_id)).decode("utf-8", "replace")

    def path_of(self, node_id):
        return self._node_to_path.get(node_id, node_id)

    @property
    def by_name(self):
        if self._by_name is None:
            index = {}
            for path, nid in self.files.items():
                for frag_id, kind, name, _path, start, end in self.fs.node_fragments(nid):
                    index.setdefault(name, []).append((path, frag_id, kind, start, end))
            self._by_name = index
        return self._by_name

    def next_pack_call_id(self):
        self._pack_calls += 1
        return f"tc_pack_{self._pack_calls}"


# --- baseline + shared tools ---------------------------------------------
def _tool_list_files(corpus):
    def handler(_args):
        paths = sorted(corpus.files)
        head = paths[:_MAX_LIST]
        out = "\n".join(head)
        if len(paths) > _MAX_LIST:
            out += f"\n... ({len(paths) - _MAX_LIST} more files omitted)"
        return out or "(empty repo)"

    return Tool(
        "list_files", "List every file path in the repository.",
        {"type": "object", "properties": {}}, handler,
    )


def _tool_read_file(corpus):
    def handler(args):
        path = args.get("path", "")
        nid = corpus.files.get(path)
        if nid is None:
            return f"error: no such file {path!r}"
        lines = corpus.read_text(nid).splitlines()
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None or end is not None:
            s = max(1, int(start or 1))
            e = min(len(lines), int(end or len(lines)))
            body = "\n".join(lines[s - 1:e])
            return f"{path} (lines {s}-{e} of {len(lines)}):\n{body}"
        return f"{path} ({len(lines)} lines):\n" + "\n".join(lines)

    return Tool(
        "read_file",
        "Read a file by path. Optionally pass start_line/end_line to read a range.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
        handler,
    )


def _tool_grep(corpus):
    def handler(args):
        pattern = args.get("pattern", "")
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for path in sorted(corpus.files):
            text = corpus.read_text(corpus.files[path])
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path}:{n}: {line.strip()}")
                    if len(hits) >= _MAX_GREP_HITS:
                        return "\n".join(hits) + "\n... (more hits omitted)"
        return "\n".join(hits) or f"(no matches for {pattern!r})"

    return Tool(
        "grep", "Search every file for a regex; returns path:line: text hits.",
        {"type": "object", "properties": {"pattern": {"type": "string"}},
         "required": ["pattern"]},
        handler,
    )


# --- rag tool -------------------------------------------------------------
def _tool_search_chunks(corpus):
    def handler(args):
        query = args.get("query", "")
        k = int(args.get("k", 4))
        terms = [t.lower() for t in re.findall(r"\w+", query)]
        scored = []
        for path in sorted(corpus.files):
            lines = corpus.read_text(corpus.files[path]).splitlines()
            for i in range(0, len(lines), _CHUNK_LINES):
                window = lines[i:i + _CHUNK_LINES]
                blob = "\n".join(window).lower()
                score = sum(blob.count(t) for t in terms)
                if score:
                    scored.append((score, path, i + 1, "\n".join(window)))
        scored.sort(key=lambda r: r[0], reverse=True)
        top = scored[:k]
        if not top:
            return f"(no chunks matched {query!r})"
        return "\n\n".join(
            f"# {path}:{start} (score {score})\n{body}" for score, path, start, body in top
        )

    return Tool(
        "search_chunks",
        "Retrieve the top-k text chunks most similar to a query (lexical RAG).",
        {"type": "object",
         "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
         "required": ["query"]},
        handler,
    )


# --- anfs structural tools ------------------------------------------------
def _tool_outline(corpus):
    def handler(args):
        path = args.get("path", "")
        nid = corpus.files.get(path)
        if nid is None:
            return f"error: no such file {path!r}"
        rows = corpus.fs.node_fragments(nid)
        if not rows:
            return f"(no symbols in {path})"
        return "\n".join(
            f"{kind} {name}  [bytes {start}-{end}]"
            for _frag, kind, name, _p, start, end in rows
        )

    return Tool(
        "outline",
        "List a file's symbols (functions, structs, ...) with byte ranges.",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "required": ["path"]},
        handler,
    )


def _tool_symbol_search(corpus):
    def handler(args):
        name = args.get("name", "")
        hits = corpus.by_name.get(name, [])
        if not hits:
            return f"(no symbol named {name!r})"
        return "\n".join(
            f"{kind} {name}  in {path}  [bytes {start}-{end}]"
            for path, _frag, kind, start, end in hits
        )

    return Tool(
        "symbol_search",
        "Find where a symbol is defined across the repo (exact name).",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"]},
        handler,
    )


def _tool_callers(corpus):
    def handler(args):
        name = args.get("name", "")
        rows = corpus.fs.fragment_callers(name)
        if not rows:
            return f"(no callers of {name!r})"
        return "\n".join(
            f"{src_kind} {src_name}  in {corpus.path_of(src_node)}  "
            f"[call at bytes {ev_start}-{ev_end}]"
            for _frag, src_kind, src_name, src_node, ev_start, ev_end in rows
        )

    return Tool(
        "callers",
        "List every call site of a symbol across the repo (its blast radius).",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"]},
        handler,
    )


def _tool_call_graph(corpus, agent_id):
    def handler(args):
        seed = args.get("name", "")
        direction = args.get("direction", "callees")
        depth = int(args.get("max_depth", 3))
        if direction not in ("callees", "callers"):
            return "error: direction must be 'callees' or 'callers'"
        nodes, edges, est = corpus.fs.call_graph(
            seed, direction, depth, 4, 2000,
            agent_id=agent_id, tool_call_id=corpus.next_pack_call_id(),
        )
        if not nodes:
            return f"(no symbol named {seed!r})"
        flow = sorted({(e[1] or "?", e[2]) for e in edges if e[3] is not None})
        external = sum(1 for e in edges if e[3] is None)
        lines = [f"call_graph({seed}, {direction}) ~{est} tokens, "
                 f"{len(nodes)} symbols, {len(flow)} resolved edges, {external} external/ambiguous"]
        lines.append("flow:")
        lines += [f"  {s} -> {d}" for s, d in flow[:40]]
        lines.append("symbols:")
        for _fid, node_id, sym, kind, _s, _e, depth_n, source in nodes:
            lines.append(f"// {corpus.path_of(node_id)}::{sym or '<anon>'} ({kind}, depth {depth_n})\n{source}")
        return "\n".join(lines)

    return Tool(
        "call_graph",
        "Walk the call graph from a symbol in one call: direction='callees' "
        "follows the execution flow downward (how it works), 'callers' is the "
        "multi-hop blast radius upward (what depends on it). Returns the call "
        "flow plus each reached symbol's source.",
        {"type": "object",
         "properties": {"name": {"type": "string"},
                        "direction": {"type": "string", "enum": ["callees", "callers"]},
                        "max_depth": {"type": "integer"}},
         "required": ["name"]},
        handler,
    )


def _tool_read_span(corpus):
    def handler(args):
        path = args.get("path", "")
        nid = corpus.files.get(path)
        if nid is None:
            return f"error: no such file {path!r}"
        start = int(args.get("start", 0))
        length = int(args.get("length", 0))
        data = bytes(corpus.fs.read_node_range(nid, start, length))
        return f"{path} [bytes {start}-{start + length}]:\n" + data.decode("utf-8", "replace")

    return Tool(
        "read_span",
        "Read an exact byte span of a file (use ranges from outline/symbol_search).",
        {"type": "object",
         "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                        "length": {"type": "integer"}},
         "required": ["path", "start", "length"]},
        handler,
    )


def _tool_context_pack(corpus, agent_id):
    def handler(args):
        name = args.get("name", "")
        budget = int(args.get("token_budget", _DEFAULT_PACK_BUDGET))
        items, estimate = corpus.fs.context_pack(
            name, budget, agent_id=agent_id, tool_call_id=corpus.next_pack_call_id()
        )
        if not items:
            return f"(no symbol named {name!r})"
        # context_pack already returns each fragment's (policy-filtered) source.
        blocks = [
            f"// {corpus.path_of(node_id)}::{sym or '<anon>'} ({kind})\n{source}"
            for node_id, _frag, sym, kind, _start, _end, source in items
        ]
        return f"context_pack({name}) ~{estimate} tokens:\n\n" + "\n\n".join(blocks)

    return Tool(
        "context_pack",
        "Get a symbol's definition plus its callers, token-bounded, in one call.",
        {"type": "object",
         "properties": {"name": {"type": "string"}, "token_budget": {"type": "integer"}},
         "required": ["name"]},
        handler,
    )


# --- arm factories --------------------------------------------------------
def baseline_toolset(corpus):
    return [_tool_list_files(corpus), _tool_read_file(corpus), _tool_grep(corpus)]


def rag_toolset(corpus):
    return [_tool_search_chunks(corpus), _tool_read_file(corpus)]


def anfs_toolset(corpus, agent_id="eval_agent"):
    return [
        _tool_list_files(corpus),
        _tool_outline(corpus),
        _tool_symbol_search(corpus),
        _tool_callers(corpus),
        _tool_call_graph(corpus, agent_id),
        _tool_context_pack(corpus, agent_id),
        _tool_read_span(corpus),
        _tool_grep(corpus),
        _tool_read_file(corpus),
    ]
