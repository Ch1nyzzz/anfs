"""SCIP-index oracle: a compiler-grade ground truth for call/reference sites.

SCIP (https://github.com/sourcegraph/scip) is one protobuf format emitted by
type-aware indexers across languages — rust-analyzer (Rust), scip-typescript,
scip-python, scip-java, scip-go, scip-clang. That makes it the single oracle
behind the C6 accuracy axis for all of codegraph's repos: ANFS and grep are
both scored for recall/precision against it.

We compare at (file, 1-based line) granularity, which needs no enclosing-symbol
attribution and matches how grep and ANFS evidence spans report sites. Symbols
are grouped by their SIMPLE name (the identifier before `(`), so the oracle's
"call sites of name X" is the union over every symbol named X — the same
name-based view ANFS resolves, making the comparison apples-to-apples.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import scip_pb2  # noqa: E402

SYMBOL_ROLE_DEFINITION = 0x1  # scip.proto SymbolRole.Definition

# Last `identifier(` in a SCIP symbol string is the method/function name.
_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(")


def generate_scip(repo_dir, out_path="index.scip", rust_analyzer="rust-analyzer"):
    """Run rust-analyzer to emit a SCIP index for a Rust project. Returns the
    absolute index path. (Other languages: scip-<lang> writing the same format.)"""
    out_abs = os.path.join(repo_dir, out_path)
    subprocess.run([rust_analyzer, "scip", ".", "--output", out_path],
                   cwd=repo_dir, check=True, capture_output=True, text=True)
    return out_abs


def _simple_name(symbol):
    matches = _NAME_RE.findall(symbol)
    return matches[-1] if matches else None


def load_index(scip_path):
    idx = scip_pb2.Index()
    with open(scip_path, "rb") as h:
        idx.ParseFromString(h.read())
    return idx


def reference_sites_by_name(scip_path):
    """{simple_name: set((relative_path, line_1based))} for every NON-definition
    occurrence — the type-resolved call/reference sites, the oracle truth."""
    refs = {}
    for doc in load_index(scip_path).documents:
        path = doc.relative_path
        for occ in doc.occurrences:
            if occ.symbol_roles & SYMBOL_ROLE_DEFINITION:
                continue  # the definition itself is not a call site
            name = _simple_name(occ.symbol)
            if name is None:
                continue
            line = occ.range[0] + 1  # SCIP lines are 0-based
            refs.setdefault(name, set()).add((path, line))
    return refs


def definition_names(scip_path):
    """Set of simple names that have a definition occurrence (functions/methods)."""
    names = set()
    for doc in load_index(scip_path).documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & SYMBOL_ROLE_DEFINITION and occ.symbol.endswith(")."):
                name = _simple_name(occ.symbol)
                if name:
                    names.add(name)
    return names
