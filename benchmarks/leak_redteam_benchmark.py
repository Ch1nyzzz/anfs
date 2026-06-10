#!/usr/bin/env python3
"""W3 privacy-leak red-team harness.

Proves the ANFS claim that *field-level policy labels block sensitive data at
the kernel layer, regardless of prompt*. The proof is a differential: the same
corpus and the same retrieval probes are run with the kernel labels active
("kernel_on") and inactive ("kernel_off"). kernel_off is the control — it is
what a plain filesystem or a vanilla RAG store does, since neither understands
field-level policy. The claim holds iff kernel_on leaks nothing on any surface
while kernel_off leaks on the content-bearing surfaces.

This harness is mechanical and deterministic: a "leak" is the sensitive byte
string actually reaching the caller (or a derived answer succeeding in citing
it). It needs no LLM. A prompt-only baseline (asking a model "do not reveal
PII") is a separate, model-dependent experiment and is intentionally NOT part
of this proof — the point of ANFS is that the guarantee does not depend on a
model complying with a prompt.

Run:
    python benchmarks/leak_redteam_benchmark.py            # built-in corpus
    python benchmarks/leak_redteam_benchmark.py --json out.json
"""

import argparse
import json
import os
import tempfile

import anfs_core


# Each document carries non-sensitive body text (so search/query can discover
# it) plus one or more sensitive fields addressed by an ANFS field path.
# `media`: "json" -> $.path syntax; "markdown" -> frontmatter.path syntax.
BUILTIN_CORPUS = [
    {
        "name": "customer_ada.json",
        "media": "json",
        "content": (
            b'{"name":"Ada Lovelace","role":"customer",'
            b'"note":"premium analytics subscriber",'
            b'"ssn":"123-45-6789","email":"ada@example.com"}'
        ),
        "body_term": "analytics",
        "fields": [
            {"path": "$.ssn", "secret": b"123-45-6789", "label": "pii"},
            {"path": "$.email", "secret": b"ada@example.com", "label": "pii"},
        ],
    },
    {
        "name": "employee_grace.json",
        "media": "json",
        "content": (
            b'{"name":"Grace Hopper","department":"compiler research",'
            b'"salary":185000,"performance":"exceeds expectations"}'
        ),
        "body_term": "compiler",
        "fields": [
            {"path": "$.salary", "secret": b"185000", "label": "confidential"},
        ],
    },
    {
        "name": "patient_alan.json",
        "media": "json",
        "content": (
            b'{"patient":"Alan T","visit":"annual checkup",'
            b'"diagnosis":"hypertension stage 1","mrn":"MRN-558831"}'
        ),
        "body_term": "checkup",
        "fields": [
            {"path": "$.diagnosis", "secret": b"hypertension stage 1", "label": "phi"},
            {"path": "$.mrn", "secret": b"MRN-558831", "label": "phi"},
        ],
    },
    {
        "name": "vendor_keys.json",
        "media": "json",
        "content": (
            b'{"service":"billing gateway","environment":"production",'
            b'"api_key":"sk-live-9af12bc7de"}'
        ),
        "body_term": "billing",
        "fields": [
            {"path": "$.api_key", "secret": b"sk-live-9af12bc7de", "label": "secret"},
        ],
    },
    {
        "name": "contact_card.md",
        "media": "markdown",
        "content": (
            b"---\n"
            b"name: Katherine Johnson\n"
            b"team: orbital mechanics\n"
            b"phone: 202-555-0147\n"
            b"---\n"
            b"Lead trajectory analyst for the orbital program.\n"
        ),
        "body_term": "trajectory",
        "fields": [
            {"path": "frontmatter.phone", "secret": b"202-555-0147", "label": "pii"},
        ],
    },
]

# Labels in the corpus that the kernel should deny when policy is active.
SENSITIVE_LABELS = ["pii", "confidential", "phi", "secret"]


def load_corpus(path):
    """Load an external corpus JSON, decoding base64-free byte fields.

    The external format mirrors BUILTIN_CORPUS but with string (not bytes)
    content/secret values; this lets a PrivacyLens-derived corpus be dropped
    in without code changes.
    """
    with open(path) as handle:
        raw = json.load(handle)
    corpus = []
    for doc in raw:
        fields = [
            {
                "path": f["path"],
                "secret": f["secret"].encode(),
                "label": f["label"],
            }
            for f in doc["fields"]
        ]
        corpus.append(
            {
                "name": doc["name"],
                "media": doc["media"],
                "content": doc["content"].encode(),
                "body_term": doc["body_term"],
                "fields": fields,
            }
        )
    return corpus


def install_corpus(fs, ws, corpus, label_fields):
    """Write every doc, publish it, and (optionally) label its sensitive fields.

    Returns a list of per-document records used by the probes.
    """
    records = []
    for idx, doc in enumerate(corpus):
        node_id = ws.write(doc["name"], doc["content"], [])
        ref_name = f"artifact:leak/{idx}/{doc['name']}@v1"
        ws.publish(doc["name"], ref_name)
        if label_fields:
            for field in doc["fields"]:
                if doc["media"] == "json":
                    fs.set_json_field_policy_label(
                        node_id,
                        field["path"],
                        field["label"],
                        "true",
                        "policy_agent",
                    )
                else:
                    fs.set_markdown_field_policy_label(
                        node_id,
                        field["path"],
                        field["label"],
                        "true",
                        "policy_agent",
                    )
        records.append(
            {
                "node_id": node_id,
                "ref_name": ref_name,
                "doc": doc,
            }
        )
    return records


def field_range(fs, node_id, media, field_path):
    """Resolve a field path to its (offset, length) byte span."""
    spans = (
        fs.json_field_spans(node_id)
        if media == "json"
        else fs.markdown_field_spans(node_id)
    )
    for row in spans:
        if row[0] == field_path:
            return row[1], row[2]
    raise KeyError(f"field {field_path} not found in {node_id}")


def probe_document(fs, reader, record):
    """Run every retrieval surface against one document's sensitive fields.

    Returns a list of probe results: (surface, leaked: bool).
    """
    node_id = record["node_id"]
    ref_name = record["ref_name"]
    doc = record["doc"]
    results = []

    for field in doc["fields"]:
        secret = field["secret"]
        offset, length = field_range(fs, node_id, doc["media"], field["path"])

        # Surface 1: whole-node read (content-bearing).
        try:
            data = bytes(fs.read_node(node_id))
            results.append(("read_node", secret in data))
        except anfs_core.PolicyDeniedError:
            results.append(("read_node", False))

        # Surface 2: targeted range read of the field (content-bearing).
        try:
            data = bytes(fs.read_node_range(node_id, offset, length))
            results.append(("read_node_range", secret in data))
        except anfs_core.PolicyDeniedError:
            results.append(("read_node_range", False))

        # Surface 3: answer citing the secret-bearing ref (derived output).
        # A successful answer means the secret flowed into a new artifact.
        try:
            reader.answer(
                f"answers/leak-{node_id[-8:]}-{field['path']}.md",
                b"synthesized answer",
                [ref_name],
            )
            results.append(("answer_cite", True))
        except anfs_core.PolicyDeniedError:
            results.append(("answer_cite", False))

    # Surface 4 (discovery): can the attacker even find the node?
    search_hits = {hit[0] for hit in reader.search(doc["body_term"], "published")}
    results.append(("search_discovery", node_id in search_hits))
    query_hits = {row[1] for row in fs.query(prefix="artifact:leak/")}
    results.append(("query_discovery", node_id in query_hits))

    return results


def run_mode(corpus, label_fields, activate_rules):
    """Install the corpus in a fresh engine and probe it under one mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        records = install_corpus(fs, writer, corpus, label_fields)

        if activate_rules:
            for label in SENSITIVE_LABELS:
                fs.set_policy_rule(
                    label,
                    value="true",
                    effect="deny",
                    scope="visibility",
                    subject_type="fragment",
                    agent_id="policy_agent",
                )

        reader = fs.open_workspace("ws:reader", "reader_agent")
        per_surface = {}
        for record in records:
            for surface, leaked in probe_document(fs, reader, record):
                bucket = per_surface.setdefault(surface, {"probes": 0, "leaks": 0})
                bucket["probes"] += 1
                bucket["leaks"] += 1 if leaked else 0

        integrity = fs.verify_integrity()
        return per_surface, integrity


CONTENT_SURFACES = {"read_node", "read_node_range", "answer_cite"}


def summarize(per_surface):
    content_probes = sum(
        v["probes"] for k, v in per_surface.items() if k in CONTENT_SURFACES
    )
    content_leaks = sum(
        v["leaks"] for k, v in per_surface.items() if k in CONTENT_SURFACES
    )
    return {
        "by_surface": per_surface,
        "content_probes": content_probes,
        "content_leaks": content_leaks,
        "content_leak_rate": (content_leaks / content_probes) if content_probes else 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description="ANFS W3 privacy-leak red-team")
    parser.add_argument(
        "--corpus",
        help="path to an external corpus JSON (defaults to the built-in corpus)",
    )
    parser.add_argument("--json", help="write the full report to this path")
    args = parser.parse_args()

    corpus = load_corpus(args.corpus) if args.corpus else BUILTIN_CORPUS

    # kernel_off: control. No labels, no rules -> plain filesystem / RAG.
    off_surface, off_integrity = run_mode(corpus, label_fields=False, activate_rules=False)
    # kernel_on: field labels + active deny rules.
    on_surface, on_integrity = run_mode(corpus, label_fields=True, activate_rules=True)

    report = {
        "documents": len(corpus),
        "kernel_off": summarize(off_surface),
        "kernel_on": summarize(on_surface),
        "integrity_clean": on_integrity == [] and off_integrity == [],
    }

    def pct(x):
        return f"{x * 100:.0f}%"

    print(f"corpus documents: {report['documents']}")
    print("\nsurface                kernel_off leaks   kernel_on leaks")
    print("-" * 58)
    all_surfaces = sorted(set(off_surface) | set(on_surface))
    for surface in all_surfaces:
        off = off_surface.get(surface, {"probes": 0, "leaks": 0})
        on = on_surface.get(surface, {"probes": 0, "leaks": 0})
        tag = "  (content)" if surface in CONTENT_SURFACES else "  (discovery)"
        print(
            f"{surface:<22} {off['leaks']:>3}/{off['probes']:<3}          "
            f"{on['leaks']:>3}/{on['probes']:<3}{tag}"
        )
    print("-" * 58)
    print(
        f"content leak rate      "
        f"{pct(report['kernel_off']['content_leak_rate']):<14}   "
        f"{pct(report['kernel_on']['content_leak_rate'])}"
    )
    print(f"\nintegrity clean: {report['integrity_clean']}")

    verdict = (
        report["kernel_on"]["content_leaks"] == 0
        and report["kernel_off"]["content_leaks"] > 0
    )
    print(
        "VERDICT: "
        + (
            "PASS — kernel labels block every content surface that the "
            "control leaks on."
            if verdict
            else "FAIL — differential not demonstrated."
        )
    )

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nwrote {args.json}")

    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
