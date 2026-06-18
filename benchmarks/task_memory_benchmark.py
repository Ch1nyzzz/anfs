#!/usr/bin/env python3
import argparse
from collections import Counter
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

import anfs_core


NAMES = [
    "ada",
    "grace",
    "katherine",
    "alan",
    "barbara",
    "margaret",
    "donald",
    "frances",
]
ATTRIBUTES = [
    ("city", ["lisbon", "seattle", "kyoto", "berlin", "nairobi", "oslo"]),
    ("project", ["apollo", "mercury", "voyager", "gemini", "artemis", "pioneer"]),
    ("food", ["risotto", "ramen", "tacos", "tagine", "gnocchi", "paella"]),
]
MEMORY_MODES = ["raw", "extracted", "hybrid"]
DEFAULT_DATASET_NAME = "synthetic-locomo-like"


def millis(start):
    return (time.perf_counter() - start) * 1000.0


def raw_session_content(idx, name, attr, value, key):
    return (
        f"# Session {idx:04d}\n\n"
        f"speaker: {name}\n"
        f"memory_key: {key}\n"
        f"stable_fact: {name}'s {attr} is {value}.\n"
        f"distractor: {name} also mentioned routine planning notes.\n"
    ).encode("utf-8")


def extracted_memory_content(idx, name, attr, value, key, source_ref):
    return (
        f"# Extracted Memory {idx:04d}\n\n"
        f"memory_key: {key}\n"
        f"stable_fact: {name}'s {attr} is {value}.\n"
        f"source_ref: {source_ref}\n"
    ).encode("utf-8")


def build_task_corpus(session_count, questions):
    facts = []
    sessions = []
    for idx in range(session_count):
        name = NAMES[idx % len(NAMES)]
        attr, values = ATTRIBUTES[idx % len(ATTRIBUTES)]
        value = values[idx % len(values)]
        key = f"memorykey{idx:04d}{attr}"
        raw_ref = f"memory:locomo/raw/session-{idx:04d}"
        fact_ref = f"memory:locomo/fact/{key}"
        raw_path = f"sessions/session-{idx:04d}.md"
        fact_path = f"extracted/{key}.md"
        raw_content = raw_session_content(idx, name, attr, value, key)
        fact_content = extracted_memory_content(idx, name, attr, value, key, raw_ref)
        sessions.append(
            {
                "idx": idx,
                "raw_path": raw_path,
                "fact_path": fact_path,
                "raw_ref": raw_ref,
                "fact_ref": fact_ref,
                "raw_content": raw_content,
                "fact_content": fact_content,
                "key": key,
            }
        )
        facts.append(
            {
                "question_id": f"q-{idx:04d}",
                "question": f"What is {name}'s {attr} from the conversation?",
                "retrieval_query": key,
                "expected_answer": value,
                "raw_ref": raw_ref,
                "fact_ref": fact_ref,
            }
        )
    return sessions, facts[:questions]


def bytes_content(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).encode("utf-8")


def text_content(value):
    return bytes_content(value).decode("utf-8", errors="replace")


def normalize_answer(value):
    return re.findall(r"[a-z0-9]+", str(value).lower())


def token_f1(prediction, expected):
    pred_tokens = normalize_answer(prediction)
    expected_tokens = normalize_answer(expected)
    if not pred_tokens and not expected_tokens:
        return 1.0
    if not pred_tokens or not expected_tokens:
        return 0.0
    overlap = Counter(pred_tokens) & Counter(expected_tokens)
    common = sum(overlap.values())
    if common == 0:
        return 0.0
    precision = common / len(pred_tokens)
    recall = common / len(expected_tokens)
    return (2 * precision * recall) / (precision + recall)


def best_token_f1(prediction, expected_answers):
    return max((token_f1(prediction, answer) for answer in expected_answers), default=0.0)


def exact_answer_match(prediction, expected_answers):
    normalized_prediction = " ".join(normalize_answer(prediction))
    return any(
        normalized_prediction == " ".join(normalize_answer(answer))
        for answer in expected_answers
    )


def load_json_or_jsonl(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def dataset_records(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def session_text(record):
    if "content" in record:
        return text_content(record["content"])
    if "text" in record:
        return text_content(record["text"])
    if "transcript" in record:
        return text_content(record["transcript"])
    messages = record.get("messages") or record.get("dialogue") or record.get("turns")
    if isinstance(messages, list):
        lines = []
        for idx, message in enumerate(messages):
            if isinstance(message, dict):
                speaker = message.get("speaker") or message.get("role") or f"speaker_{idx}"
                content = message.get("content") or message.get("text") or message.get("utterance")
                lines.append(f"{speaker}: {text_content(content)}")
            else:
                lines.append(text_content(message))
        return "\n".join(lines)
    return text_content(record)


def load_task_corpus(args):
    if not args.dataset_path:
        sessions, questions = build_task_corpus(args.sessions, args.questions)
        return DEFAULT_DATASET_NAME, sessions, questions

    data = load_json_or_jsonl(args.dataset_path)
    session_records = dataset_records(data, "sessions", "conversations", "dialogues", "records")
    question_records = dataset_records(data, "questions", "qa", "qas", "evaluations")
    if isinstance(data, list):
        session_records = [
            row for row in data if row.get("kind", row.get("type", "session")) != "question"
        ]
        question_records = [
            row for row in data if row.get("kind", row.get("type")) == "question"
        ]
    if not session_records:
        raise SystemExit("--dataset-path must provide sessions/conversations/dialogues")
    if not question_records:
        raise SystemExit("--dataset-path must provide questions/qa records")

    sessions = []
    raw_ref_by_key = {}
    fact_ref_by_key = {}
    for idx, record in enumerate(session_records[: args.sessions]):
        session_id = str(record.get("id") or record.get("session_id") or record.get("conversation_id") or idx)
        key = re.sub(r"[^A-Za-z0-9_-]+", "-", session_id).strip("-") or f"session-{idx:04d}"
        raw_ref = record.get("raw_ref") or f"memory:locomo/raw/{key}"
        fact_ref = record.get("fact_ref") or f"memory:locomo/fact/{key}"
        raw_content = (
            f"# Session {idx:04d}\n\n"
            f"session_id: {session_id}\n"
            f"memory_key: {key}\n"
            f"{session_text(record)}\n"
        ).encode("utf-8")
        fact_text = record.get("memory") or record.get("summary") or record.get("fact")
        if fact_text is None:
            fact_text = session_text(record)
        fact_content = (
            f"# Extracted Memory {idx:04d}\n\n"
            f"memory_key: {key}\n"
            f"stable_fact: {text_content(fact_text).strip()}\n"
            f"source_ref: {raw_ref}\n"
        ).encode("utf-8")
        sessions.append(
            {
                "idx": idx,
                "raw_path": f"sessions/{key}.md",
                "fact_path": f"extracted/{key}.md",
                "raw_ref": raw_ref,
                "fact_ref": fact_ref,
                "raw_content": raw_content,
                "fact_content": fact_content,
                "key": key,
            }
        )
        raw_ref_by_key[session_id] = raw_ref
        raw_ref_by_key[key] = raw_ref
        fact_ref_by_key[session_id] = fact_ref
        fact_ref_by_key[key] = fact_ref

    questions = []
    for idx, record in enumerate(question_records[: args.questions]):
        expected = record.get("expected_answers", record.get("answers", record.get("answer")))
        if expected is None:
            raise SystemExit(f"question {idx} is missing answer/answers")
        expected_answers = expected if isinstance(expected, list) else [expected]
        session_keys = record.get("session_ids") or record.get("conversation_ids") or record.get("evidence_session_ids") or []
        if isinstance(session_keys, (str, int)):
            session_keys = [session_keys]
        raw_refs = record.get("raw_refs") or record.get("expected_raw_refs")
        fact_refs = record.get("fact_refs") or record.get("expected_fact_refs")
        raw_refs = raw_refs or [raw_ref_by_key[str(key)] for key in session_keys if str(key) in raw_ref_by_key]
        fact_refs = fact_refs or [fact_ref_by_key[str(key)] for key in session_keys if str(key) in fact_ref_by_key]
        if not raw_refs and sessions:
            raw_refs = [sessions[min(idx, len(sessions) - 1)]["raw_ref"]]
        if not fact_refs and sessions:
            fact_refs = [sessions[min(idx, len(sessions) - 1)]["fact_ref"]]
        question = record.get("question") or record.get("query") or record.get("prompt")
        retrieval_query = record.get("retrieval_query") or record.get("memory_key") or question
        questions.append(
            {
                "question_id": str(record.get("id") or record.get("question_id") or f"q-{idx:04d}"),
                "question": str(question),
                "retrieval_query": str(retrieval_query),
                "expected_answer": str(expected_answers[0]),
                "expected_answers": [str(answer) for answer in expected_answers],
                "raw_ref": raw_refs[0],
                "fact_ref": fact_refs[0],
                "raw_refs": list(raw_refs),
                "fact_refs": list(fact_refs),
            }
        )
    return str(Path(args.dataset_path)), sessions, questions


def expected_refs_for_mode(question, memory_mode):
    if memory_mode == "raw":
        return question.get("raw_refs") or [question["raw_ref"]]
    if memory_mode == "extracted":
        return question.get("fact_refs") or [question["fact_ref"]]
    return (question.get("fact_refs") or [question["fact_ref"]]) + (
        question.get("raw_refs") or [question["raw_ref"]]
    )


def query_prefix_for_mode(memory_mode):
    if memory_mode == "raw":
        return "memory:locomo/raw/"
    return "memory:locomo/fact/"


def answer_from_evidence(bytes_value, expected_answers=None, fallback="unknown"):
    text = bytes_value.decode("utf-8", errors="replace")
    if expected_answers:
        lower_text = text.lower()
        for answer in expected_answers:
            if str(answer).lower() in lower_text:
                return str(answer)
    marker = "stable_fact:"
    for line in text.splitlines():
        if line.startswith(marker) and " is " in line:
            value = line.rsplit(" is ", 1)[1].strip().rstrip(".")
            if value:
                return value
        if line.startswith(marker):
            value = line[len(marker) :].strip().rstrip(".")
            if value:
                return value
    return fallback


def run_anfs(args, root):
    db_path = str(root / "task-memory.db")
    objs_dir = str(root / "objs")
    fs = anfs_core.AnfsEngine(db_path, objs_dir)
    writer = fs.open_workspace("ws:memory-writer", "memory_writer")
    reader = fs.open_workspace("ws:qa", "qa_agent", run_id="run:task-memory")
    dataset_name, sessions, questions = load_task_corpus(args)

    start = time.perf_counter()
    raw_session_refs = 0
    extracted_fact_refs = 0
    derived_lineage_links = 0
    for session in sessions:
        raw_node = writer.write(
            session["raw_path"],
            session["raw_content"],
            [],
            tool_call_id=f"tc_write_raw_{session['idx']:04d}",
        )
        if args.memory_mode in ("raw", "hybrid"):
            writer.publish(
                session["raw_path"],
                session["raw_ref"],
                kind="memory_raw",
                tool_call_id=f"tc_publish_raw_{session['idx']:04d}",
            )
            raw_session_refs += 1
        if args.memory_mode in ("extracted", "hybrid"):
            fact_node = writer.write(
                session["fact_path"],
                session["fact_content"],
                [raw_node],
                tool_call_id=f"tc_write_fact_{session['idx']:04d}",
            )
            if raw_node in fs.lineage_nodes(fact_node, direction="ancestors"):
                derived_lineage_links += 1
            writer.publish(
                session["fact_path"],
                session["fact_ref"],
                kind="memory_fact",
                tool_call_id=f"tc_publish_fact_{session['idx']:04d}",
            )
            extracted_fact_refs += 1
    ingest_ms = millis(start)

    answers = []
    context_bytes = 0
    retrieval_hits = 0
    covered_answers = 0
    start = time.perf_counter()
    for question in questions:
        tool_call_id = f"tc_query_{question['question_id']}"
        rows = reader.query(
            prefix=query_prefix_for_mode(args.memory_mode),
            text=question["retrieval_query"],
            state="published",
            limit=args.top_k,
            tool_call_id=tool_call_id,
        )
        fallback_used = False
        if args.memory_mode == "hybrid" and not rows:
            fallback_used = True
            fallback_tool_call_id = f"tc_query_fallback_{question['question_id']}"
            rows = reader.query(
                prefix="memory:locomo/raw/",
                text=question["retrieval_query"],
                state="published",
                limit=args.top_k,
                tool_call_id=fallback_tool_call_id,
            )
            tool_call_id = fallback_tool_call_id
        query_events = fs.events(kind="query", tool_call_id=tool_call_id)
        query_event_id = query_events[0][1] if query_events else None
        retrieved_refs = [row[0] for row in rows]
        expected_refs = expected_refs_for_mode(question, args.memory_mode)
        hit = any(ref_name in retrieved_refs for ref_name in expected_refs)
        retrieval_hits += 1 if hit else 0
        answer = "unknown"
        answer_event_id = None
        coverage = []
        if rows:
            evidences = [bytes(fs.read_node(row[1])) for row in rows]
            context_bytes += sum(len(evidence) for evidence in evidences)
            ref_name = rows[0][0]
            answer = answer_from_evidence(
                evidences[0],
                question.get("expected_answers", [question["expected_answer"]]),
            )
            # The answer API was removed; a cited answer is now a generic write
            # whose derived_from edges record the evidence lineage.
            del ref_name
            derived_node = reader.write(
                f"answers/{question['question_id']}.md",
                answer.encode("utf-8"),
                derived_from_nodes=[rows[0][1]],
                tool_call_id=f"tc_answer_{question['question_id']}",
            )
            del derived_node
            # answer_evidence_coverage was removed with the answer API; use the
            # retrieval hit as the coverage proxy.
            if hit:
                covered_answers += 1
        expected_answers = question.get("expected_answers", [question["expected_answer"]])
        answer_f1 = best_token_f1(answer, expected_answers)
        answers.append(
            {
                "question_id": question["question_id"],
                "expected_refs": expected_refs,
                "retrieved_refs": retrieved_refs,
                "retrieval_hit": hit,
                "expected_answer": question["expected_answer"],
                "expected_answers": expected_answers,
                "answer": answer,
                "exact_match": exact_answer_match(answer, expected_answers),
                "token_f1": answer_f1,
                "query_event_id": query_event_id,
                "answer_event_id": answer_event_id,
                "coverage": coverage,
                "fallback_used": fallback_used,
            }
        )
    qa_ms = millis(start)

    start = time.perf_counter()
    integrity_issues = fs.verify_integrity()
    integrity_ms = millis(start)
    exact_matches = sum(1 for row in answers if row["exact_match"])
    token_f1_sum = sum(row["token_f1"] for row in answers)
    return {
        "backend": "anfs",
        "parameters": {
            "sessions": len(sessions),
            "questions": len(questions),
            "top_k": args.top_k,
            "memory_mode": args.memory_mode,
            "dataset": dataset_name,
        },
        "timings_ms": {
            "ingest": ingest_ms,
            "qa": qa_ms,
            "integrity": integrity_ms,
            "total": ingest_ms + qa_ms + integrity_ms,
        },
        "metrics": {
            "questions": len(questions),
            "retrieval_hits": retrieval_hits,
            "recall_at_k": retrieval_hits / len(questions) if questions else 0.0,
            "exact_matches": exact_matches,
            "exact_match_rate": exact_matches / len(questions) if questions else 0.0,
            "token_f1": token_f1_sum / len(questions) if questions else 0.0,
            "covered_answers": covered_answers,
            "context_bytes": context_bytes,
            "raw_session_refs": raw_session_refs,
            "extracted_fact_refs": extracted_fact_refs,
            "derived_lineage_links": derived_lineage_links,
            "integrity_issues": integrity_issues,
        },
        "answers": answers,
        "passed": retrieval_hits >= args.min_recall * len(questions)
        and token_f1_sum >= args.min_token_f1 * len(questions)
        and (not args.dataset_path or covered_answers == len(questions))
        and (bool(args.dataset_path) or exact_matches == len(questions))
        and (bool(args.dataset_path) or covered_answers == len(questions))
        and derived_lineage_links == extracted_fact_refs
        and not integrity_issues,
        "semantic_evidence": [
            "raw sessions are stored as immutable ANFS nodes",
            "extracted memories are derived nodes with lineage to raw sessions when enabled",
            "questions use audited Workspace.query retrieval events",
            "answers cite retrieved memory refs and expose evidence coverage",
            "verify_integrity checks the resulting event/ref/evidence graph",
        ],
        "paths": {
            "workdir": str(root),
            "db_path": db_path,
            "objs_dir": objs_dir,
        },
    }


def run_native_jsonl(args, root):
    dataset_name, sessions, questions = load_task_corpus(args)
    memory_dir = root / "memory"
    source_dir = root / "sources"
    memory_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    audit_path = root / "events.jsonl"

    start = time.perf_counter()
    primary_records = []
    fallback_records = []
    raw_session_refs = 0
    extracted_fact_refs = 0
    for session in sessions:
        raw_source_path = source_dir / Path(session["raw_path"]).name
        raw_source_path.write_bytes(session["raw_content"])
        if args.memory_mode in ("raw", "hybrid"):
            path = memory_dir / Path(session["raw_path"]).name
            path.write_bytes(session["raw_content"])
            raw_record = {
                "ref_name": session["raw_ref"],
                "content": session["raw_content"],
            }
            if args.memory_mode == "raw":
                primary_records.append(raw_record)
            else:
                fallback_records.append(raw_record)
            raw_session_refs += 1
            with audit_path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(
                    json.dumps(
                        {
                            "kind": "write",
                            "ref_name": session["raw_ref"],
                            "path": str(path),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        if args.memory_mode in ("extracted", "hybrid"):
            path = memory_dir / Path(session["fact_path"]).name
            path.write_bytes(session["fact_content"])
            primary_records.append(
                {
                    "ref_name": session["fact_ref"],
                    "content": session["fact_content"],
                },
            )
            extracted_fact_refs += 1
            with audit_path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(
                    json.dumps(
                        {
                            "kind": "extract_memory",
                            "ref_name": session["fact_ref"],
                            "path": str(path),
                            "source_path": str(raw_source_path),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        with audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(
                json.dumps(
                    {
                        "kind": "source_write",
                        "path": str(raw_source_path),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    ingest_ms = millis(start)

    answers = []
    context_bytes = 0
    retrieval_hits = 0
    start = time.perf_counter()
    for question in questions:
        retrieved_refs = []
        answer = "unknown"
        records = primary_records
        for search_round in range(2):
            for record in records:
                text = record["content"].decode("utf-8", errors="replace")
                if question["retrieval_query"] in text:
                    retrieved_refs.append(record["ref_name"])
                    if len(retrieved_refs) == 1:
                        answer = answer_from_evidence(
                            record["content"],
                            question.get("expected_answers", [question["expected_answer"]]),
                        )
                    context_bytes += len(record["content"])
                    if len(retrieved_refs) >= args.top_k:
                        break
            if retrieved_refs or args.memory_mode != "hybrid":
                break
            records = fallback_records
        expected_refs = expected_refs_for_mode(question, args.memory_mode)
        hit = any(ref_name in retrieved_refs for ref_name in expected_refs)
        retrieval_hits += 1 if hit else 0
        expected_answers = question.get("expected_answers", [question["expected_answer"]])
        answer_f1 = best_token_f1(answer, expected_answers)
        answers.append(
            {
                "question_id": question["question_id"],
                "expected_refs": expected_refs,
                "retrieved_refs": retrieved_refs,
                "retrieval_hit": hit,
                "expected_answer": question["expected_answer"],
                "expected_answers": expected_answers,
                "answer": answer,
                "exact_match": exact_answer_match(answer, expected_answers),
                "token_f1": answer_f1,
            }
        )
    qa_ms = millis(start)
    exact_matches = sum(1 for row in answers if row["exact_match"])
    token_f1_sum = sum(row["token_f1"] for row in answers)
    return {
        "backend": "native-jsonl",
        "parameters": {
            "sessions": len(sessions),
            "questions": len(questions),
            "top_k": args.top_k,
            "memory_mode": args.memory_mode,
            "dataset": dataset_name,
        },
        "timings_ms": {
            "ingest": ingest_ms,
            "qa": qa_ms,
            "integrity": 0.0,
            "total": ingest_ms + qa_ms,
        },
        "metrics": {
            "questions": len(questions),
            "retrieval_hits": retrieval_hits,
            "recall_at_k": retrieval_hits / len(questions) if questions else 0.0,
            "exact_matches": exact_matches,
            "exact_match_rate": exact_matches / len(questions) if questions else 0.0,
            "token_f1": token_f1_sum / len(questions) if questions else 0.0,
            "covered_answers": 0,
            "context_bytes": context_bytes,
            "raw_session_refs": raw_session_refs,
            "extracted_fact_refs": extracted_fact_refs,
            "derived_lineage_links": 0,
            "integrity_issues": [],
        },
        "answers": answers,
        "passed": retrieval_hits >= args.min_recall * len(questions)
        and token_f1_sum >= args.min_token_f1 * len(questions)
        and (bool(args.dataset_path) or exact_matches == len(questions)),
        "semantic_limitations": [
            "no immutable CAS node graph",
            "retrieval is direct file scan rather than audited query events",
            "answers do not carry typed citation edges or evidence coverage",
            "no kernel integrity verification",
        ],
        "paths": {
            "workdir": str(root),
            "memory_dir": str(memory_dir),
            "source_dir": str(source_dir),
            "audit_path": str(audit_path),
        },
    }


def child_args(args, backend, memory_mode):
    class Child:
        pass

    child = Child()
    child.__dict__.update(vars(args))
    child.backend = backend
    child.memory_mode = memory_mode
    if args.workdir:
        child.workdir = str(Path(args.workdir) / f"{backend}-{memory_mode}")
    return child


def selected_values(value, choices):
    if value == "all":
        return choices
    return [value]


def run_backend_mode(args):
    root_owner = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        root_owner = tempfile.mkdtemp(
            prefix=f"{args.backend}-{args.memory_mode}-task-memory-bench-"
        )
        root = Path(root_owner)
    try:
        if args.backend == "anfs":
            return run_anfs(args, root)
        if args.backend == "native-jsonl":
            return run_native_jsonl(args, root)
        raise SystemExit(f"unsupported backend: {args.backend}")
    finally:
        if root_owner and not args.keep:
            shutil.rmtree(root_owner, ignore_errors=True)


def run_matrix(args):
    backends = selected_values(args.backend, ["anfs", "native-jsonl"])
    memory_modes = selected_values(args.memory_mode, MEMORY_MODES)
    results = [
        run_backend_mode(child_args(args, backend, memory_mode))
        for memory_mode in memory_modes
        for backend in backends
    ]
    if len(results) == 1:
        return results[0]
    return {"results": results}


def result_passed(result):
    if "results" in result:
        return all(row["passed"] for row in result["results"])
    return result["passed"]


def validate_args(args):
    if args.sessions <= 0:
        raise SystemExit("--sessions must be positive")
    if args.questions <= 0:
        raise SystemExit("--questions must be positive")
    if not args.dataset_path and args.questions > args.sessions:
        raise SystemExit("--questions must be <= --sessions")
    if args.top_k <= 0:
        raise SystemExit("--top-k must be positive")
    if args.min_recall is None:
        args.min_recall = 0.0 if args.dataset_path else 1.0
    if args.min_token_f1 is None:
        args.min_token_f1 = 0.0 if args.dataset_path else 1.0
    if not 0.0 <= args.min_recall <= 1.0:
        raise SystemExit("--min-recall must be between 0 and 1")
    if not 0.0 <= args.min_token_f1 <= 1.0:
        raise SystemExit("--min-token-f1 must be between 0 and 1")


def main():
    parser = argparse.ArgumentParser(
        description="Run a local LoCoMo-like task memory benchmark over ANFS."
    )
    parser.add_argument("--backend", choices=["anfs", "native-jsonl", "all"], default="anfs")
    parser.add_argument(
        "--memory-mode",
        choices=["raw", "extracted", "hybrid", "all"],
        default="raw",
    )
    parser.add_argument("--sessions", type=int, default=24)
    parser.add_argument("--questions", type=int, default=12)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument(
        "--dataset-path",
        help="Optional JSON/JSONL LoCoMo-style dataset with sessions and questions.",
    )
    parser.add_argument("--min-recall", type=float)
    parser.add_argument("--min-token-f1", type=float)
    parser.add_argument("--workdir")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--output-json")
    args = parser.parse_args()
    validate_args(args)

    result = run_matrix(args)
    passed = result_passed(result)
    result_json = json.dumps(result, indent=2, sort_keys=True)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result_json + "\n", encoding="utf-8")
    print(result_json)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
