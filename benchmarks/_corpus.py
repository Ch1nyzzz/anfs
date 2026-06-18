"""Corpus loaders for the C5 token-efficiency eval.

Clones a real OSS repo (shallow, cached) and indexes a subset into ANFS so
the three arms navigate genuine code, mirroring codegraph's real-repo
methodology. v1 targets Tokio — it is on codegraph's own benchmark list AND
is Rust, the only language ANFS currently parses for defines/calls.

The shallow clone is cached under the system temp dir, so repeated benchmark
runs do not re-clone.
"""

import os
import subprocess
import tempfile

from _anfs_tools import Corpus

TOKIO_URL = "https://github.com/tokio-rs/tokio"
DEFAULT_SUBDIR = "tokio/src/runtime"  # the async scheduler — codegraph's tokio question


def ensure_tokio(dest=None):
    """Shallow-clone tokio once into a cache dir; return the clone root."""
    dest = dest or os.path.join(tempfile.gettempdir(), "anfs_bench_tokio")
    if not os.path.isdir(os.path.join(dest, ".git")):
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", TOKIO_URL, dest],
            check=True, capture_output=True, text=True,
        )
    return dest


def read_subset(root, subdir=DEFAULT_SUBDIR, exts=(".rs",), max_files=None):
    """Read repo-relative {path: text} for `*exts` files under `root/subdir`."""
    base = os.path.join(root, subdir)
    if not os.path.isdir(base):
        raise FileNotFoundError(f"subset not found: {base}")
    sources = {}
    for dirpath, _dirs, files in os.walk(base):
        for fn in sorted(files):
            if not fn.endswith(exts):
                continue
            full = os.path.join(dirpath, fn)
            try:
                text = open(full, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            sources[os.path.relpath(full, root)] = text
            if max_files and len(sources) >= max_files:
                return sources
    return sources


def load_tokio_runtime(fs, root=None, subdir=DEFAULT_SUBDIR, max_files=None):
    """Clone (if needed), read the subset, and index it into ANFS.

    Returns (corpus, sources).
    """
    root = ensure_tokio(root) if root is None else root
    sources = read_subset(root, subdir=subdir, max_files=max_files)
    if not sources:
        raise RuntimeError(f"no source files under {subdir}")
    return Corpus.index(fs, sources), sources


# The seven codegraph benchmark repos, one question each. `parser` is the ANFS
# fragment parser; `scip` is how the compiler oracle index is produced.
REPOS = {
    "tokio": {"lang": "rust", "url": "https://github.com/tokio-rs/tokio",
              "subdir": "tokio/src/runtime", "exts": (".rs",),
              "parser": "tree-sitter-rust", "scip": "rust-analyzer",
              "question": "How does tokio schedule and run async tasks on its runtime?"},
    "django": {"lang": "python", "url": "https://github.com/django/django",
               "subdir": "django/db/models", "exts": (".py",),
               "parser": "tree-sitter-python", "scip": "scip-python",
               "question": "How does Django's ORM build and execute a query from a QuerySet?"},
    "excalidraw": {"lang": "typescript", "url": "https://github.com/excalidraw/excalidraw",
                   "subdir": "packages/excalidraw", "exts": (".ts", ".tsx"),
                   "parser": "tree-sitter-typescript", "scip": "scip-typescript",
                   "question": "How does Excalidraw render and update canvas elements?"},
    "vscode": {"lang": "typescript", "url": "https://github.com/microsoft/vscode",
               "subdir": "src/vs/workbench/services", "exts": (".ts",),
               "parser": "tree-sitter-typescript", "scip": "scip-typescript",
               "question": "How does the extension host communicate with the main process?"},
    "gin": {"lang": "go", "url": "https://github.com/gin-gonic/gin",
            "subdir": ".", "exts": (".go",),
            "parser": "tree-sitter-go", "scip": "scip-go",
            "question": "How does gin route requests through its middleware chain?"},
    "okhttp": {"lang": "java", "url": "https://github.com/square/okhttp",
               "subdir": "okhttp/src/main/java", "exts": (".java",),
               "parser": "tree-sitter-java", "scip": "scip-java",
               "question": "How does OkHttp process a request through its interceptor chain?"},
    "alamofire": {"lang": "swift", "url": "https://github.com/Alamofire/Alamofire",
                  "subdir": "Source", "exts": (".swift",),
                  "parser": "tree-sitter-swift", "scip": "sourcekit-lsp",
                  "question": "How does Alamofire build, send, and validate a request?"},
}


def clone(url, dest):
    if not os.path.isdir(os.path.join(dest, ".git")):
        subprocess.run(["git", "clone", "--depth", "1", "--single-branch", url, dest],
                       check=True, capture_output=True, text=True)
    return dest


def load_repo(fs, root, subdir, parser, exts=(".rs",), max_files=None):
    """Read `*exts` under root/subdir and index into ANFS with `parser`."""
    sources = read_subset(root, subdir=subdir, exts=exts, max_files=max_files)
    if not sources:
        raise RuntimeError(f"no source files under {subdir}")
    return Corpus.index(fs, sources, parser=parser), sources
