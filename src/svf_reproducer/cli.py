from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .candidates import build_candidates
from .config import load_mapping, load_profile, read_json, write_json
from .experiments import create_comparison_plan
from .fewshot import build_index
from .layout import analyze_record_layouts
from .locking import LockUnavailable
from .models import Issue
from .service import (
    SamePathError,
    apply_and_generate,
    execute_job,
    generate_form,
    import_form,
    list_targets,
    load_annotations,
    mapping_hash,
    open_acceptance,
    propose_changes,
    render_reference,
    resolve_changes,
    save_annotations,
    write_runtime_result,
)
from .source import extract_source
from .storage import JobLedger
from .validation import create_evidence_manifest, record_check, structural_report
from .xmlcore import CompilationError, extract_ir


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svf-reproducer")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("profile-validate")
    command.add_argument("profile")
    command.add_argument("--runtime", action="store_true")
    command = sub.add_parser("mapping-hash")
    command.add_argument("mapping")
    command = sub.add_parser("fewshot-index")
    command.add_argument("source")
    command.add_argument("output")
    command = sub.add_parser("import")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("workspace")
    command.add_argument("--revision")
    command = sub.add_parser("targets")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("--kind", action="append")
    command = sub.add_parser("generate")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("changes")
    command.add_argument("output")
    command.add_argument("--workspace")
    command.add_argument("--revision")
    command.add_argument("--mapping")
    command = sub.add_parser("validate")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("--mapping")
    command.add_argument("--output")
    command.add_argument("--form-revision")
    command = sub.add_parser("layout")
    command.add_argument("xml")
    command.add_argument("profile")
    command = sub.add_parser("render-test")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("output")
    command.add_argument("--mode", choices=["design", "data"], default="design")
    command.add_argument("--data-count", type=int)
    command = sub.add_parser("record-check")
    command.add_argument("report")
    command.add_argument("kind", choices=["structure", "preview", "runtime", "designer"])
    command.add_argument("status", choices=["pass", "fail", "not_run"])
    command.add_argument("evidence", nargs="*")
    command = sub.add_parser("evidence-create")
    command.add_argument("report")
    command.add_argument("kind", choices=["structure", "preview", "runtime", "designer"])
    command.add_argument("output")
    command.add_argument("artifacts", nargs="+")
    command.add_argument("--note", default="")
    command.add_argument("--runtime-result")
    command.add_argument("--metadata", action="append", default=[], help="designer証拠のkey=value。product、version、operator、resultが必要")
    command = sub.add_parser("extract")
    command.add_argument("source")
    command.add_argument("profile")
    command.add_argument("output")
    command.add_argument("--dpi", type=float)
    command = sub.add_parser("candidates")
    command.add_argument("source_json")
    command.add_argument("output")
    command = sub.add_parser("comparison-plan")
    command.add_argument("output")
    command.add_argument("--capacity", type=int)
    command = sub.add_parser("annotation-add")
    command.add_argument("file")
    command.add_argument("source_hash")
    command.add_argument("text")
    command.add_argument("target_ids", nargs="+")
    command = sub.add_parser("annotation-revise")
    command.add_argument("file")
    command.add_argument("annotation_id")
    command.add_argument("source_hash")
    command.add_argument("text")
    command.add_argument("--target-id", action="append", default=[])
    command = sub.add_parser("annotation-cancel")
    command.add_argument("file")
    command.add_argument("annotation_id")
    command = sub.add_parser("annotation-list")
    command.add_argument("file")
    command = sub.add_parser("resolve")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("annotations")
    command.add_argument("--output")
    command = sub.add_parser("propose")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("annotations")
    command.add_argument("output")
    command.add_argument("--fewshot-index")
    command.add_argument("--target-id", action="append", default=[])
    command = sub.add_parser("proposal-apply")
    command.add_argument("xml")
    command.add_argument("profile")
    command.add_argument("proposal")
    command.add_argument("annotations")
    command.add_argument("output")
    command.add_argument("--workspace")
    command.add_argument("--revision")
    command.add_argument("--mapping")
    command = sub.add_parser("run")
    command.add_argument("form")
    command.add_argument("job")
    command.add_argument("mapping")
    command.add_argument("profile")
    command.add_argument("workspace")
    command.add_argument("output_dir")
    command.add_argument("--result-output")
    command = sub.add_parser("acceptance-open")
    command.add_argument("workspace")
    command.add_argument("revision")
    command = sub.add_parser("status")
    command.add_argument("workspace")
    command.add_argument("--job-id")
    command = sub.add_parser("reconcile")
    command.add_argument("workspace")
    command.add_argument("job_id")
    command.add_argument("status", choices=["SUCCEEDED", "FAILED", "EMPTY", "UNKNOWN"])
    command.add_argument("--operator", required=True)
    command.add_argument("--evidence", required=True, help="外部ジョブ照会結果、または未送信と確認できた根拠")
    sub.add_parser("gui")
    return parser


def _metadata(items: list[str]) -> dict[str, str]:
    value: dict[str, str] = {}
    for item in items:
        key, _, body = item.partition("=")
        if not key or not body:
            raise ValueError(f"--metadataはkey=value形式が必要です: {item!r}")
        value[key] = body
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "profile-validate":
            issues = load_profile(args.profile).readiness_issues(args.runtime)
            _print({"issues": [x.to_dict() for x in issues]})
            return 2 if issues else 0
        if args.command == "mapping-hash":
            print(mapping_hash(args.mapping))
            return 0
        if args.command == "fewshot-index":
            value = build_index(args.source)
            write_json(args.output, value)
            _print({"examples": len(value["examples"]), "output": str(Path(args.output).resolve())})
            return 0
        if args.command == "import":
            path = import_form(args.xml, args.profile, args.workspace, args.revision)
            _print({"revision": str(path)})
            return 0
        if args.command == "targets":
            _print({"targets": list_targets(args.xml, args.profile, set(args.kind) if args.kind else None)})
            return 0
        if args.command == "generate":
            value = read_json(args.changes)
            changes = value.get("changes", value)
            if not isinstance(changes, list):
                raise ValueError("changes.jsonにはchanges配列が必要です")
            report = generate_form(args.xml, args.profile, changes, args.output, workspace=args.workspace, revision=args.revision, mapping_path=args.mapping)
            _print(report)
            return 3 if report["checks"][0]["status"] == "fail" else 0
        if args.command == "validate":
            profile = load_profile(args.profile)
            mapping = load_mapping(args.mapping) if args.mapping else None
            report = structural_report(args.xml, profile, mapping, form_revision=args.form_revision)
            if args.output:
                write_json(args.output, report)
            _print(report)
            return 3 if report["checks"][0]["status"] == "fail" else 0
        if args.command == "layout":
            profile = load_profile(args.profile)
            ir = extract_ir(args.xml, profile)
            layouts, issues = analyze_record_layouts(ir, profile)
            _print({"layouts": [x.to_dict() for x in layouts], "issues": [x.to_dict() for x in ir.issues + issues]})
            return 3 if any(x.severity == "error" for x in ir.issues + issues) else 0
        if args.command == "render-test":
            issues = render_reference(args.xml, args.profile, args.output, mode=args.mode, data_count=args.data_count)
            _print({"output": str(Path(args.output).resolve()), "issues": [x.to_dict() for x in issues]})
            return 3 if any(x.severity == "error" for x in issues) else 0
        if args.command == "record-check":
            report = record_check(args.report, args.kind, args.status, args.evidence)
            _print(report)
            return 0 if args.status != "fail" else 3
        if args.command == "evidence-create":
            manifest = create_evidence_manifest(
                args.report,
                args.kind,
                args.artifacts,
                args.output,
                args.note,
                metadata=_metadata(args.metadata),
                runtime_result_path=args.runtime_result,
            )
            _print(manifest)
            return 0
        if args.command == "extract":
            value, issues = extract_source(args.source, load_profile(args.profile), source_dpi=args.dpi)
            value["issues"] = [x.to_dict() for x in issues]
            write_json(args.output, value)
            _print({"output": str(Path(args.output).resolve()), "issues": value["issues"]})
            return 3 if any(x.severity == "error" for x in issues) else 0
        if args.command == "candidates":
            value, issues = build_candidates(read_json(args.source_json))
            value["issues"] = [x.to_dict() for x in issues]
            write_json(args.output, value)
            _print({"output": str(Path(args.output).resolve()), "issues": value["issues"]})
            return 0
        if args.command == "comparison-plan":
            value = create_comparison_plan(args.capacity)
            write_json(args.output, value)
            _print(value)
            return 0
        if args.command in {"annotation-add", "annotation-revise", "annotation-cancel"}:
            notes = load_annotations(args.file)
            if args.command == "annotation-add":
                note = notes.add(args.target_ids, args.text, args.source_hash)
            elif args.command == "annotation-revise":
                note = notes.revise(args.annotation_id, args.text, args.source_hash, args.target_id or None)
            else:
                note = notes.cancel(args.annotation_id)
            save_annotations(args.file, notes)
            _print(note.to_dict())
            return 0
        if args.command == "annotation-list":
            _print(load_annotations(args.file).to_dict())
            return 0
        if args.command == "resolve":
            notes = load_annotations(args.annotations)
            changes, issues = resolve_changes(args.xml, args.profile, notes)
            value = {"changes": changes, "issues": [x.to_dict() for x in issues]}
            if args.output:
                write_json(args.output, value)
            _print(value)
            return 3 if any(x.severity == "error" for x in issues) else 0
        if args.command == "propose":
            notes = load_annotations(args.annotations)
            index = read_json(args.fewshot_index) if args.fewshot_index else None
            proposal = propose_changes(args.xml, args.profile, notes, fewshot_index=index, target_ids=args.target_id or None)
            write_json(args.output, proposal)
            _print({"output": str(Path(args.output).resolve()), "changes": len(proposal.get("changes", []))})
            return 0
        if args.command == "proposal-apply":
            notes = load_annotations(args.annotations)
            proposal = read_json(args.proposal)
            report = apply_and_generate(
                args.xml,
                args.profile,
                proposal,
                notes,
                args.output,
                workspace=args.workspace,
                revision=args.revision,
                mapping_path=args.mapping,
            )
            _print(report)
            return 3 if report["checks"][0]["status"] == "fail" else 0
        if args.command == "run":
            result = execute_job(args.form, args.job, args.mapping, args.profile, args.workspace, args.output_dir)
            if args.result_output:
                write_runtime_result(result, args.result_output)
            _print(result.to_dict())
            return {"SUCCEEDED": 0, "EMPTY": 0, "FAILED": 4, "UNKNOWN": 5}[result.status]
        if args.command == "acceptance-open":
            path = open_acceptance(args.workspace, args.revision)
            _print({"report": str(path)})
            return 0
        if args.command == "status":
            jobs = JobLedger(args.workspace).entries()
            if args.job_id:
                jobs = {args.job_id: jobs.get(args.job_id)} if args.job_id in jobs else {}
            _print({"jobs": jobs})
            return 0 if jobs or not args.job_id else 2
        if args.command == "reconcile":
            entry = JobLedger(args.workspace).reconcile(args.job_id, args.status, operator=args.operator, evidence=args.evidence)
            _print(entry)
            return 0
        if args.command == "gui":
            from .gui import main as gui_main

            gui_main()
            return 0
    except CompilationError as exc:
        _print({"issues": [x.to_dict() for x in exc.issues]})
        return 3
    except SamePathError as exc:
        _print({"issues": [Issue("OUTPUT_PATH_CONFLICT", str(exc), stage="generate").to_dict()]})
        return 2
    except LockUnavailable as exc:
        _print({"issues": [Issue("WORKSPACE_BUSY", str(exc), stage="runtime").to_dict()]})
        return 5
    except (OSError, ValueError, KeyError, TimeoutError, json.JSONDecodeError) as exc:
        _print({"issues": [{"code": "INPUT_INVALID", "reason": str(exc), "severity": "error", "stage": "cli", "target_id": None}]})
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
