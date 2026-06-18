"""SourceKit-LSP oracle for Swift — the compiler-grade reference truth there is
no SCIP indexer for. SourceKit is Swift's own compiler frontend, so its
`textDocument/references` is the same class of ground truth rust-analyzer/SCIP
give the other languages; we just drive it over LSP and emit the same
{simple_name: set((relative_path, line_1based))} shape as scip_oracle.

Requires the package to be built first (`swift build`) so SourceKit's index
store is populated. Single-file/whole-project; references are cross-file.
"""

import json
import os
import subprocess
import time
import urllib.parse

SYMBOL_FUNCTIONISH = {6, 9, 12}  # LSP SymbolKind: Method, Constructor, Function


class _LSP:
    def __init__(self, root, server="sourcekit-lsp"):
        self.root = os.path.realpath(root)  # resolve /tmp -> /private/tmp
        self.proc = subprocess.Popen(
            [server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, cwd=self.root,
        )
        self._id = 0

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.proc.stdin.flush()

    def _read_msg(self):
        headers = b""
        while b"\r\n\r\n" not in headers:
            ch = self.proc.stdout.read(1)
            if not ch:
                return None
            headers += ch
        length = 0
        for line in headers.decode().split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = b""
        while len(body) < length:
            chunk = self.proc.stdout.read(length - len(body))
            if not chunk:
                break
            body += chunk
        return json.loads(body)

    def request(self, method, params, timeout=120):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._read_msg()
            if msg is None:
                return None
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg.get("result")
            # a server->client request needs a reply or it may block; answer null
            if "id" in msg and "method" in msg:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        return None

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def uri(self, path):
        return "file://" + urllib.parse.quote(os.path.join(self.root, path))

    def rel(self, uri):
        p = os.path.realpath(urllib.parse.unquote(uri[len("file://"):]))
        return os.path.relpath(p, self.root)

    def shutdown(self):
        try:
            self.request("shutdown", None, timeout=10)
            self.notify("exit", None)
        except Exception:
            pass
        self.proc.terminate()


def reference_sites_by_name(root, source_paths, server="sourcekit-lsp", index_wait=45):
    """{simple_name: set((relative_path, line_1based))} of every reference of
    every function/method/constructor defined under source_paths.

    Needs `swift build` run first (index store) AND background indexing enabled +
    given time to settle, or references come back empty.
    """
    lsp = _LSP(root, server)
    lsp.request("initialize", {
        "processId": os.getpid(), "rootUri": lsp.uri(""),
        "capabilities": {}, "workspaceFolders": [{"uri": lsp.uri(""), "name": "root"}],
        "initializationOptions": {"backgroundIndexing": True},
    })
    lsp.notify("initialized", {})
    time.sleep(index_wait)  # let background indexing settle

    refs = {}
    for rel in source_paths:
        uri = lsp.uri(rel)
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        lsp.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "swift", "version": 1, "text": text}})
        syms = lsp.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}) or []

        # flatten hierarchical DocumentSymbols
        stack = list(syms)
        flat = []
        while stack:
            s = stack.pop()
            flat.append(s)
            stack.extend(s.get("children", []) or [])

        for s in flat:
            if s.get("kind") not in SYMBOL_FUNCTIONISH:
                continue
            name = s.get("name", "").split("(")[0]
            sel = (s.get("selectionRange") or s.get("range") or {}).get("start")
            if not name or not sel:
                continue
            locs = lsp.request("textDocument/references", {
                "textDocument": {"uri": uri}, "position": sel,
                "context": {"includeDeclaration": False},
            }) or []
            for loc in locs:
                refs.setdefault(name, set()).add(
                    (lsp.rel(loc["uri"]), loc["range"]["start"]["line"] + 1))
    lsp.shutdown()
    return refs
