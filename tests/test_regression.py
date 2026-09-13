"""レビューの再現ケースR01〜R20を、受入条件を期待値とする回帰試験へ移したもの。

観察された修正前の動作を正解にしない。各章の受入条件を期待値とする。
実SVF、実Windows、Designer、印刷、外部ネットワーク送信は実行しない。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from svf_reproducer import locking
from svf_reproducer.annotations import AnnotationSet
from svf_reproducer.candidates import build_candidates
from svf_reproducer.config import load_profile, sha256_file, stable_json_hash, write_json
from svf_reproducer.jobs import run_runtime, runtime_input_hash, validate_job
from svf_reproducer.layout import analyze_record_layouts
from svf_reproducer.models import Job, MappingConfig, MappingField, RuntimeResult, TargetProfile
from svf_reproducer.preview import render_svg
from svf_reproducer.proposals import apply_proposal
from svf_reproducer.service import SamePathError, execute_job, generate_form
from svf_reproducer.storage import JobLedger, load_current
from svf_reproducer.validation import accepted, acceptance_issues, create_evidence_manifest, record_check, structural_report
from svf_reproducer.xmldoc import XmlSecurityError, load_document, parse_xml
from svf_reproducer.xmlcore import CompilationError, compile_xml, extract_ir, validate_xml

from test_core import SAMPLE_XML, make_profile


def errors(issues) -> list[str]:
    return [x.code for x in issues if x.severity == "error"]


class RegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.xml = self.root / "form.xml"
        self.xml.write_bytes(SAMPLE_XML)
        self.profile = make_profile(self.xml)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(self, name: str, data: bytes = SAMPLE_XML):
        path = self.root / f"{name}.xml"
        path.write_bytes(data)
        return path, make_profile(path)

    def profile_file(self, profile: TargetProfile, name: str = "profile") -> Path:
        path = self.root / f"{name}.json"
        write_json(path, profile.to_dict())
        return path

    # --- 4章 F01 誤った対象への変更を防ぐ ---------------------------------
    def test_R01_batch_edits_only_the_named_original_elements(self) -> None:
        path, profile = self.fixture("ids", b'<FormData>\n  <Line y1="10"/>\n  <Line y1="20"/>\n  <Line y1="30"/>\n</FormData>\n')
        profile.compatibility["Line"] = "edit_supported"
        profile.editable_attributes["Line"] = ["y1"]
        profile.element_templates["line"] = '<Line y1="40"/>'
        ir = extract_ir(path, profile)
        lines = [x for x in ir.elements if x.kind == "Line"]
        body = compile_xml(
            path,
            [
                {"op": "remove", "target_id": lines[0].id},
                {"op": "add", "target_id": ir.elements[0].id, "value": {"template": "line"}},
                {"op": "set", "target_id": lines[1].id, "property": "y1", "value": 555},
            ],
            profile,
        )
        self.assertEqual(["555", "30", "40"], [x.get("y1") for x in parse_xml(body).findall("Line")])

    def test_R01_reparent_then_set_edits_the_same_entity(self) -> None:
        data = b'<FormData>\n  <Record name="A">\n    <Text name="T" x="1" y="1" strText="x"/>\n  </Record>\n  <Record name="B"/>\n</FormData>\n'
        path, profile = self.fixture("move", data)
        profile.allow_reparent = True
        profile.compatibility.update({"Text": "edit_supported", "Record": "edit_supported"})
        profile.editable_attributes["Text"] = ["y"]
        ir = extract_ir(path, profile)
        text = next(x for x in ir.elements if x.name == "T")
        target = next(x for x in ir.elements if x.name == "B")
        body = compile_xml(
            path,
            [
                {"op": "reparent", "target_id": text.id, "value": {"new_parent_id": target.id}},
                {"op": "set", "target_id": text.id, "property": "y", "value": 99},
            ],
            profile,
        )
        root = parse_xml(body)
        self.assertEqual([], list(root.find('./Record[@name="A"]')))
        self.assertEqual("99", root.find('./Record[@name="B"]/Text').attrib["y"])

    def test_R01_set_after_removing_the_parent_leaves_the_batch_unapplied(self) -> None:
        profile = make_profile(self.xml)
        profile.compatibility["SubForm"] = "edit_supported"
        ir = extract_ir(self.xml, profile)
        subform = next(x for x in ir.elements if x.name == "DETAIL")
        record = next(x for x in ir.elements if x.name == "R1")
        with self.assertRaises(CompilationError) as caught:
            compile_xml(
                self.xml,
                [
                    {"op": "remove", "target_id": subform.id},
                    {"op": "set", "target_id": record.id, "property": "displayLineCount", "value": 9},
                ],
                profile,
            )
        self.assertIn("TARGET_REMOVED", [x.code for x in caught.exception.issues])
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())

    def test_R18_proposal_must_stay_inside_the_annotation_scope(self) -> None:
        data = SAMPLE_XML.replace(b'<Field name="HEADER"', b'<Record name="R2" displayLineCount="2"/><Field name="HEADER"')
        path, profile = self.fixture("scope", data)
        ir = extract_ir(path, profile)
        first = next(x for x in ir.elements if x.name == "R1")
        second = next(x for x in ir.elements if x.name == "R2")
        notes = AnnotationSet()
        note = notes.add([first.id], "表示行数を8行に変更", ir.source_hash)

        def proposal(changes):
            return {
                "source_hash": ir.source_hash,
                "base_revision": ir.revision,
                "annotation_revisions": notes.active_revisions(),
                "changes": changes,
                "xml_fragments": [],
                "unresolved": [],
            }

        outside = [{"target_id": second.id, "op": "set", "property": "displayLineCount", "value": 8, "note_ids": [note.id], "example_ids": []}]
        with self.assertRaises(CompilationError) as caught:
            apply_proposal(path, proposal(outside), source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=profile)
        self.assertIn("PROPOSAL_OUT_OF_SCOPE", [x.code for x in caught.exception.issues])
        self.assertEqual(data, path.read_bytes())

    def test_R18_unknown_cancelled_and_stale_notes_are_rejected(self) -> None:
        ir = extract_ir(self.xml, self.profile)
        record = next(x for x in ir.elements if x.kind == "Record")
        notes = AnnotationSet()
        note = notes.add([record.id], "表示行数を8行に変更", ir.source_hash)
        stale = notes.add([record.id], "表示行数を8行に変更", "old-source-hash")

        def run(note_ids):
            proposal = {
                "source_hash": ir.source_hash,
                "base_revision": ir.revision,
                "annotation_revisions": notes.active_revisions(),
                "changes": [{"target_id": record.id, "op": "set", "property": "displayLineCount", "value": 8, "note_ids": note_ids, "example_ids": []}],
                "xml_fragments": [],
                "unresolved": [],
            }
            with self.assertRaises(CompilationError) as caught:
                apply_proposal(self.xml, proposal, source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=self.profile)
            return [x.code for x in caught.exception.issues]

        self.assertIn("ANNOTATION_UNKNOWN", run(["not-a-note"]))
        self.assertIn("ANNOTATION_STALE", run([stale.id]))
        self.assertIn("PROPOSAL_SCOPE_UNKNOWN", run([]))
        notes.cancel(note.id)
        proposal = {
            "source_hash": ir.source_hash,
            "base_revision": ir.revision,
            "annotation_revisions": notes.active_revisions(),
            "changes": [{"target_id": record.id, "op": "set", "property": "displayLineCount", "value": 8, "note_ids": [note.id], "example_ids": []}],
            "xml_fragments": [],
            "unresolved": [],
        }
        with self.assertRaises(CompilationError) as caught:
            apply_proposal(self.xml, proposal, source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=self.profile)
        self.assertIn("ANNOTATION_CANCELLED", [x.code for x in caught.exception.issues])

    def test_R18_multiple_targets_in_scope_change_nothing_else(self) -> None:
        data = SAMPLE_XML.replace(
            b'<SubForm name="OVERFLOW"',
            b'<SubForm name="SECOND" x1="100" y1="1400" x2="1000" y2="1800" direction="1">'
            b'<Record name="R2" x1="100" y1="1400" x2="1000" y2="1500" heightUnit="0" height="100" displayLineCount="2"/>'
            b"</SubForm>\n  <SubForm name=\"OVERFLOW\"",
        )
        path, profile = self.fixture("multi", data)
        ir = extract_ir(path, profile)
        first = next(x for x in ir.elements if x.name == "R1")
        second = next(x for x in ir.elements if x.name == "R2")
        notes = AnnotationSet()
        note = notes.add([first.id, second.id], "表示行数を3行に変更", ir.source_hash)
        proposal = {
            "source_hash": ir.source_hash,
            "base_revision": ir.revision,
            "annotation_revisions": notes.active_revisions(),
            "changes": [
                {"target_id": first.id, "op": "set", "property": "displayLineCount", "value": 3, "note_ids": [note.id], "example_ids": []},
                {"target_id": second.id, "op": "set", "property": "displayLineCount", "value": 3, "note_ids": [note.id], "example_ids": []},
            ],
            "xml_fragments": [],
            "unresolved": [],
        }
        body = apply_proposal(path, proposal, source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=profile)
        before = {(x.id, tuple(sorted(x.attributes.items()))) for x in ir.elements}
        after_ir = extract_ir(path, profile, data=body)
        after = {(x.id, tuple(sorted(x.attributes.items()))) for x in after_ir.elements}
        difference = {x[0] for x in before ^ after}
        self.assertEqual({first.id, second.id}, difference)
        self.assertEqual(["3", "3"], [x.attributes["displayLineCount"] for x in after_ir.elements if x.kind == "Record"])

    # --- 5章 F02・F03 正本を壊さない ---------------------------------------
    def test_R10_failed_structure_keeps_old_output_and_current(self) -> None:
        profile = make_profile(self.xml)
        profile.compatibility["SubForm"] = "edit_supported"
        profile.editable_attributes["SubForm"] = ["x2"]
        profile_path = self.profile_file(profile)
        workspace = self.root / "work"
        record = next(x for x in extract_ir(self.xml, profile).elements if x.kind == "Record")
        output = self.root / "generated.xml"
        good = generate_form(self.xml, profile_path, [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": 6}], output, workspace=workspace, revision="good")
        self.assertEqual("pass", good["checks"][0]["status"])
        previous = output.read_bytes()
        subform = next(x for x in extract_ir(self.xml, profile).elements if x.name == "DETAIL")
        report = generate_form(self.xml, profile_path, [{"op": "set", "target_id": subform.id, "property": "x2", "value": -999}], output, workspace=workspace, revision="bad")
        self.assertEqual("fail", report["checks"][0]["status"])
        self.assertFalse(report["committed"])
        self.assertFalse(report["output_written"])
        self.assertEqual(previous, output.read_bytes())
        self.assertEqual("good", load_current(workspace).name)
        self.assertFalse((workspace / "revisions" / "bad").exists())
        self.assertTrue(Path(report["diagnostic_path"]).is_dir())

    def test_R11_same_input_and_output_path_is_refused(self) -> None:
        profile_path = self.profile_file(self.profile)
        record = next(x for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        change = [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": 12}]
        with self.assertRaises(SamePathError):
            generate_form(self.xml, profile_path, change, self.xml, workspace=self.root / "same")
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())
        link = self.root / "alias.xml"
        link.symlink_to(self.xml)
        with self.assertRaises(SamePathError):
            generate_form(self.xml, profile_path, change, link)
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())

    def test_R11_saved_source_is_the_original_not_the_generated(self) -> None:
        profile_path = self.profile_file(self.profile)
        record = next(x for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        workspace = self.root / "work"
        report = generate_form(
            self.xml,
            profile_path,
            [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": 12}],
            self.root / "out.xml",
            workspace=workspace,
            revision="r001",
        )
        revision = Path(report["revision_path"])
        self.assertEqual(SAMPLE_XML, (revision / "source.xml").read_bytes())
        self.assertNotEqual(SAMPLE_XML, (revision / "generated.xml").read_bytes())

    def test_R04_attribute_edit_preserves_every_other_byte(self) -> None:
        source = Path(__file__).resolve().parents[1] / "few-shot.txt"
        profile = make_profile(source)
        ir = extract_ir(source, profile)
        record = next(x for x in ir.elements if x.kind == "Record")
        before = source.read_bytes()
        after = compile_xml(source, [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": 12}], profile)
        self.assertEqual(before.count(b"\r\n"), after.count(b"\r\n"))
        self.assertEqual(before.splitlines()[0], after.splitlines()[0])
        self.assertEqual(before.endswith(b"\n"), after.endswith(b"\n"))
        old_document, new_document = load_document(before), load_document(after)
        self.assertEqual(old_document.encoding, new_document.encoding)
        changed = [
            (x.attributes, y.attributes)
            for x, y in zip(extract_ir(source, profile, data=before).elements, extract_ir(source, profile, data=after).elements)
            if x.attributes != y.attributes
        ]
        self.assertEqual(1, len(changed))
        self.assertEqual("12", changed[0][1]["displayLineCount"])

    def test_R04_unchanged_document_returns_the_original_bytes(self) -> None:
        self.assertEqual(SAMPLE_XML, compile_xml(self.xml, [], self.profile))

    # --- 6章 F04・F05 入力の取り違えと重複出力を防ぐ -----------------------
    def job_fixture(self, name: str):
        path, profile = self.fixture(name)
        csv_path = self.root / f"{name}.csv"
        csv_path.write_bytes(b"code\r\nA\r\n")
        mapping = MappingConfig("m", [MappingField("code", "detail", "CODE", record_id="R1", required=True)])
        job = Job(name, "rev", stable_json_hash(mapping.to_dict()), {}, str(csv_path), 1, profile.profile_id)
        return path, profile, csv_path, mapping, job

    def test_R08_changing_csv_content_changes_the_input_identity(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("csvhash")
        first = runtime_input_hash(path, job, mapping, profile)
        csv_path.write_bytes(b"code\r\nB\r\n")
        second = runtime_input_hash(path, job, mapping, profile)
        self.assertNotEqual(first, second)
        ledger = JobLedger(self.root / "ledger")
        ledger.record(job.job_id, first, RuntimeResult("SUCCEEDED", "old-A-result", 1))
        replay = ledger.claim(job.job_id, second)
        self.assertEqual(["JOB_ID_CONFLICT"], [x.code for x in replay])

    def test_R09_timeout_records_unknown_without_leaving_running(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("timeout")
        profile.runtime_kind = "command"
        profile.runtime_timeout_seconds = 0.3
        profile.runtime_command = [sys.executable, "-c", 'import time; print("started", flush=True); time.sleep(5)']
        workspace = self.root / "timeout-workspace"
        write_json(workspace / "revisions" / "rev" / "manifest.json", {"revision": "rev", "files": {}})
        (workspace / "revisions" / "rev" / "source.xml").write_bytes(path.read_bytes())
        paths = {"profile": self.profile_file(profile, "timeout-profile"), "mapping": self.root / "timeout-mapping.json", "job": self.root / "timeout-job.json"}
        write_json(paths["mapping"], mapping.to_dict())
        write_json(paths["job"], job.to_dict())
        result = execute_job(path, paths["job"], paths["mapping"], paths["profile"], workspace, self.root / "timeout-output")
        self.assertEqual("UNKNOWN", result.status)
        self.assertEqual("RUNTIME_TIMEOUT", result.error_code)
        self.assertTrue(Path(result.log_paths[0]).read_text().startswith("started"))
        entry = JobLedger(workspace).entries()[job.job_id]
        self.assertEqual("FINISHED", entry["state"])
        self.assertIsNotNone(entry["started_at"])
        self.assertIsNotNone(entry["host"])
        self.assertIsNotNone(entry["pid"])
        replay = JobLedger(workspace).claim(job.job_id, entry["input_hash"])
        self.assertEqual(["JOB_RESULT_UNKNOWN"], [x.code for x in replay])

    def test_R09_reconcile_needs_an_operator_and_external_evidence(self) -> None:
        ledger = JobLedger(self.root / "reconcile")
        ledger.record("job-1", "hash-a", RuntimeResult("UNKNOWN", "runtime-1", 2, error_code="RUNTIME_TIMEOUT"))
        with self.assertRaises(ValueError):
            ledger.reconcile("job-1", "FAILED", operator="", evidence="")
        entry = ledger.reconcile("job-1", "FAILED", operator="受入担当", evidence="外部ジョブ照会: 未送信を確認")
        self.assertEqual("FAILED", entry["result"]["status"])
        self.assertEqual("受入担当", entry["reconciliations"][0]["operator"])
        self.assertIsNone(ledger.before_run("job-1", "hash-a"))

    def test_R20_workspace_allows_only_one_running_job(self) -> None:
        ledger = JobLedger(self.root / "parallel")
        self.assertIsNone(ledger.claim("a", "hash-a"))
        blocked = ledger.claim("b", "hash-b")
        self.assertEqual(["WORKSPACE_BUSY"], [x.code for x in blocked])
        ledger.record("a", "hash-a", RuntimeResult("SUCCEEDED", "runtime-a", 1))
        self.assertIsNone(ledger.claim("b", "hash-b"))

    def test_R19_empty_pdf_is_not_a_success(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("emptypdf")
        profile.runtime_kind = "command"
        profile.runtime_command = [sys.executable, "-c", 'from pathlib import Path; import sys; Path(sys.argv[1], "empty.pdf").write_bytes(b"")', "{output_dir}"]
        result = run_runtime(path, job, mapping, profile, self.root / "emptypdf-output")
        self.assertEqual("FAILED", result.status)
        self.assertEqual("RUNTIME_ARTIFACT_INVALID", result.error_code)
        self.assertIn("ARTIFACT_EMPTY", [x.code for x in result.issues])

    def test_R19_truncated_pdf_is_not_a_success(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("badpdf")
        profile.runtime_kind = "command"
        profile.runtime_command = [sys.executable, "-c", 'from pathlib import Path; import sys; Path(sys.argv[1], "bad.pdf").write_bytes(b"not a pdf at all")', "{output_dir}"]
        result = run_runtime(path, job, mapping, profile, self.root / "badpdf-output")
        self.assertEqual("FAILED", result.status)
        self.assertIn("ARTIFACT_INVALID", [x.code for x in result.issues])

    def test_form_revision_must_match_the_committed_generation(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("revision")
        workspace = self.root / "revision-workspace"
        paths = {"profile": self.profile_file(profile, "revision-profile"), "mapping": self.root / "revision-mapping.json", "job": self.root / "revision-job.json"}
        write_json(paths["mapping"], mapping.to_dict())
        write_json(paths["job"], job.to_dict())
        result = execute_job(path, paths["job"], paths["mapping"], paths["profile"], workspace, self.root / "revision-output")
        self.assertEqual("FORM_REVISION_MISSING", result.error_code)

    # --- 7章 F06・F07 合格判定と項目契約 -----------------------------------
    def test_R06_evidence_from_an_older_xml_cannot_pass(self) -> None:
        path, profile = self.fixture("stale")
        report_path = self.root / "stale-report.json"
        write_json(report_path, structural_report(path, profile))
        evidence = self.root / "designer.txt"
        evidence.write_text("対象Designerで開いて保存した", encoding="utf-8")
        manifest = self.root / "stale-evidence.json"
        create_evidence_manifest(
            report_path,
            "designer",
            [evidence],
            manifest,
            metadata={"product": "SVFX-Designer", "version": "test-version", "operator": "受入担当", "result": "開いて保存できた"},
        )
        path.write_bytes(SAMPLE_XML.replace(b'displayLineCount="4"', b'displayLineCount="99"'))
        with self.assertRaises(ValueError):
            record_check(report_path, "designer", "pass", [manifest])

    def test_R06_history_of_an_older_generation_is_kept(self) -> None:
        path, profile = self.fixture("history")
        report_path = self.root / "history-report.json"
        write_json(report_path, structural_report(path, profile, form_revision="r001"))
        evidence = self.root / "history-designer.txt"
        evidence.write_text("対象Designerで開いて保存した", encoding="utf-8")
        manifest = self.root / "history-evidence.json"
        create_evidence_manifest(
            report_path,
            "designer",
            [evidence],
            manifest,
            metadata={"product": "SVFX-Designer", "version": "test-version", "operator": "受入担当", "result": "開いて保存できた"},
        )
        report = record_check(report_path, "designer", "pass", [manifest])
        self.assertTrue(accepted(report, {"designer"}))
        self.assertEqual("r001", report["form_revision"])
        self.assertEqual("pass", next(x for x in report["checks"] if x["kind"] == "designer")["status"])

    def test_R07_manifest_without_artifacts_cannot_pass(self) -> None:
        path, profile = self.fixture("empty-evidence")
        report_path = self.root / "empty-report.json"
        report = structural_report(path, profile)
        write_json(report_path, report)
        manifest = self.root / "empty-manifest.json"
        write_json(manifest, {"schema": "svf-evidence/1.0", "kind": "runtime", "input_hashes": report["input_hashes"], "artifacts": []})
        with self.assertRaises(ValueError):
            record_check(report_path, "runtime", "pass", [manifest])
        self.assertFalse(accepted(report, {"runtime"}))
        self.assertIn("CHECK_NOT_PASSED", [x.code for x in acceptance_issues(report, {"runtime"})])

    def test_R07_runtime_evidence_needs_a_succeeded_result(self) -> None:
        path, profile = self.fixture("runtime-evidence")
        report_path = self.root / "runtime-report.json"
        write_json(report_path, structural_report(path, profile))
        artifact = self.root / "output.pdf"
        artifact.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n")
        result_path = self.root / "runtime-result.json"
        write_json(result_path, RuntimeResult("FAILED", "runtime-1", 1, [str(artifact.resolve())], input_manifest_hash="abc").to_dict())
        with self.assertRaises(ValueError):
            create_evidence_manifest(report_path, "runtime", [artifact], self.root / "runtime-evidence.json", runtime_result_path=result_path)
        write_json(result_path, RuntimeResult("SUCCEEDED", "runtime-1", 1, [str(artifact.resolve())], input_manifest_hash="abc").to_dict())
        manifest = create_evidence_manifest(report_path, "runtime", [artifact], self.root / "runtime-evidence.json", runtime_result_path=result_path)
        self.assertEqual("abc", manifest["runtime"]["input_manifest_hash"])

    def test_R12_wrong_root_nan_and_link_cycle_are_errors(self) -> None:
        cases = {
            "wrong-root": (b'<NotForm version="9.2"/>', "ROOT_INVALID"),
            "nan": (SAMPLE_XML.replace(b'x="120"', b'x="NaN"'), "GEOMETRY_UNRESOLVED"),
            "link-cycle": (SAMPLE_XML.replace(b'name="OVERFLOW" linkName=""', b'name="OVERFLOW" linkName="DETAIL"'), "LINK_CYCLE"),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(case=name):
                path, profile = self.fixture(name, data)
                self.assertIn(expected, errors(validate_xml(path, profile)))

    def test_R12_ambiguous_field_reference_stops_the_mapping(self) -> None:
        data = SAMPLE_XML.replace(b'<Field name="CODE" x="120"', b'<Field name="CODE" x="600" charCount="8"/><Field name="CODE" x="120"')
        path, profile = self.fixture("ambiguous", data)
        mapping = MappingConfig("m", [MappingField("code", "detail", "CODE", record_id="R1")])
        self.assertIn("NAME_AMBIGUOUS", errors(validate_xml(path, profile, mapping)))

    def test_R13_display_line_count_rejects_float_and_bool(self) -> None:
        record = next(x for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        for value in (10.7, True, "10.7", 0, -1, "１０.５"):
            with self.subTest(value=value):
                with self.assertRaises(CompilationError) as caught:
                    compile_xml(self.xml, [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": value}], self.profile)
                self.assertIn("DISPLAY_LINE_COUNT_INVALID", [x.code for x in caught.exception.issues])
        body = compile_xml(self.xml, [{"op": "set", "target_id": record.id, "property": "displayLineCount", "value": "１２"}], self.profile)
        self.assertEqual("12", parse_xml(body).find(".//Record").attrib["displayLineCount"])

    def test_non_integer_attributes_keep_their_exact_value(self) -> None:
        data = SAMPLE_XML.replace(b'heightUnit="0" height="100"', b'heightUnit="0" height="100" autoLinkPitch="1.2"')
        path, profile = self.fixture("floats", data)
        profile.editable_attributes["Record"] = ["displayLineCount", "autoLinkPitch"]
        record = next(x for x in extract_ir(path, profile).elements if x.kind == "Record")
        body = compile_xml(path, [{"op": "set", "target_id": record.id, "property": "autoLinkPitch", "value": 1.35}], profile)
        self.assertEqual("1.35", parse_xml(body).find(".//Record").attrib["autoLinkPitch"])
        for bad in (float("nan"), float("inf"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(CompilationError):
                    compile_xml(path, [{"op": "set", "target_id": record.id, "property": "autoLinkPitch", "value": bad}], profile)

    def test_R13_profile_types_are_checked_at_the_load_boundary(self) -> None:
        for broken in ({"coordinate_dpi": "400"}, {"coordinate_dpi": float("nan")}, {"runtime_kind": "rest"}, {"allow_reparent": "yes"}, {"unknown_key": 1}):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    TargetProfile.from_dict({"profile_id": "p", **broken})

    def test_R16_missing_required_cell_is_an_error(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("missingcell")
        mapping.fields.append(MappingField("required", "detail", "REQUIRED", record_id="R1", required=True))
        job.mapping_hash = stable_json_hash(mapping.to_dict())
        csv_path.write_bytes(b"code,required\r\nA\r\n")
        rows, issues = validate_job(job, mapping, profile)
        self.assertIn("CSV_CELL_MISSING", errors(issues))
        self.assertIn("VALUE_MISSING", errors(issues))

    def test_R16_duplicate_and_unexpected_columns_are_errors(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("columns")
        csv_path.write_bytes(b"code,code,extra\r\nA,B,C\r\n")
        _, issues = validate_job(job, mapping, profile)
        self.assertIn("CSV_HEADER_DUPLICATE", errors(issues))
        self.assertIn("CSV_COLUMN_UNEXPECTED", errors(issues))

    def test_R16_value_lexical_rules_are_enforced(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("lexical")
        mapping = MappingConfig("m", [MappingField("amount", "detail", "CODE", record_id="R1", value_type="decimal"), MappingField("day", "detail", "CODE2", record_id="R1", value_type="date")])
        job.mapping_hash = stable_json_hash(mapping.to_dict())
        csv_path.write_bytes("amount,day\r\n1e5,2026/01/02\r\nNaN,2026-13-40\r\n".encode())
        job.data_count = 2
        _, issues = validate_job(job, mapping, profile)
        self.assertEqual(4, errors(issues).count("VALUE_TYPE_INVALID"))

    def test_embedded_newline_is_separated_from_the_record_separator(self) -> None:
        path, profile, csv_path, mapping, job = self.job_fixture("newline")
        csv_path.write_bytes(b'code\r\n"a\r\nb"\r\n')
        _, issues = validate_job(job, mapping, profile)
        self.assertIn("NEWLINE_UNSUPPORTED", errors(issues))
        self.assertNotIn("CSV_LINE_ENDING_INVALID", errors(issues))
        profile.supports_embedded_newlines = True
        _, issues = validate_job(job, mapping, profile)
        self.assertEqual([], errors(issues))

    # --- 8章 F08・F09 尺度と入力処理 ---------------------------------------
    def test_R05_dpi_mismatch_between_xml_and_profile_is_an_error(self) -> None:
        path, profile = self.fixture("dpi")
        profile.coordinate_dpi = 600
        issues = validate_xml(path, profile)
        self.assertIn("COORDINATE_DPI_MISMATCH", errors(issues))
        self.assertEqual({}, next(x for x in extract_ir(path, profile).elements if x.name == "CODE").geometry_mm)

    def test_R05_matching_dpi_keeps_the_same_millimetres(self) -> None:
        path, profile = self.fixture("dpi-ok")
        self.assertAlmostEqual(7.62, next(x for x in extract_ir(path, profile).elements if x.name == "CODE").geometry_mm["x"], places=4)

    def test_R03_shift_jis_declaration_is_read(self) -> None:
        data = '<?xml version="1.0" encoding="Shift_JIS"?>\r\n<FormData version="9.2"><Text name="T" x="1" y="1" strText="日本語"/></FormData>\r\n'.encode("shift_jis")
        path, profile = self.fixture("sjis", data)
        ir = extract_ir(path, profile)
        self.assertEqual("shift_jis", ir.encoding)
        self.assertEqual("日本語", next(x for x in ir.elements if x.kind == "Text").text)
        self.assertEqual(data, compile_xml(path, [], profile))

    def test_R14_dtd_and_entities_are_rejected_in_every_encoding(self) -> None:
        body = '<?xml version="1.0" encoding="{codec}"?><!DOCTYPE FormData [<!ENTITY tiny "EXPANDED">]><FormData><Text>&tiny;</Text></FormData>'
        for codec, encoder in (("UTF-16", "utf-16"), ("UTF-8", "utf-8"), ("Shift_JIS", "shift_jis")):
            with self.subTest(codec=codec):
                with self.assertRaises(XmlSecurityError):
                    load_document(body.format(codec=codec).encode(encoder))

    def test_unsupported_encoding_declaration_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            load_document('<?xml version="1.0" encoding="KOI8-R"?><FormData/>'.encode("ascii"))

    def test_R02_locking_works_without_fcntl_on_the_windows_path(self) -> None:
        locked: list[int] = []

        class FakeMsvcrt(types.ModuleType):
            LK_NBLCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(_fileno, mode, _size):
                if mode == FakeMsvcrt.LK_NBLCK:
                    if locked:
                        raise OSError("already locked")
                    locked.append(1)
                else:
                    locked.clear()

        original_platform, original_module = locking.IS_WINDOWS, sys.modules.get("msvcrt")
        sys.modules["msvcrt"] = FakeMsvcrt("msvcrt")
        locking.IS_WINDOWS = True
        locking.reset_backend()
        try:
            with locking.file_lock(self.root / "windows.lock"):
                self.assertEqual([1], locked)
            self.assertEqual([], locked)
            locking.fsync_directory(self.root)
        finally:
            locking.IS_WINDOWS = original_platform
            if original_module is None:
                sys.modules.pop("msvcrt", None)
            else:
                sys.modules["msvcrt"] = original_module
            locking.reset_backend()

    def test_record_pitch_uses_the_confirmed_height_unit(self) -> None:
        path, profile = self.fixture("pitch", SAMPLE_XML.replace(b'linkName="OVERFLOW"', b'linkName=""'))
        profile.enum_maps["record.heightUnit"] = {"0": "dot"}
        ir = extract_ir(path, profile)
        layouts, _ = analyze_record_layouts(ir, profile)
        self.assertAlmostEqual(6.35, layouts[0].pitch_mm, places=4)
        self.assertEqual(4, layouts[0].capacity)
        profile.enum_maps.pop("record.heightUnit")
        layouts, issues = analyze_record_layouts(extract_ir(path, profile), profile)
        self.assertIsNone(layouts[0].capacity)
        self.assertEqual("pitch_unconfirmed", layouts[0].capacity_reason)
        self.assertIn("PITCH_UNCONFIRMED", [x.code for x in issues])

    # --- 9章 F11・F12 表示と備考 -------------------------------------------
    def test_R17_design_preview_repeats_record_children(self) -> None:
        data = SAMPLE_XML.replace(b'<Field name="CODE"', b'<Text name="LABEL" x="120" y="420" strText="row-label"/><Field name="CODE"')
        path, profile = self.fixture("preview", data)
        ir = extract_ir(path, profile)
        code_id = next(x.id for x in ir.elements if x.name == "CODE")
        record_id = next(x.id for x in ir.elements if x.kind == "Record")
        design, issues = render_svg(ir, profile, mode="design")
        self.assertEqual(4, design.count(f'data-object-id="{record_id}" data-instance='), "D=4 のRecord枠が4回描かれる")
        self.assertEqual(4, design.count("row-label"))
        self.assertEqual(4, design.count(f'data-object-id="{code_id}"'))
        self.assertEqual([], [x.code for x in issues if x.code == "PREVIEW_TRUNCATED"])

    def test_R17_zero_data_draws_no_detail_row(self) -> None:
        path, profile = self.fixture("zero")
        ir = extract_ir(path, profile)
        zero, issues = render_svg(ir, profile, mode="data", data_count=0)
        self.assertEqual(0, zero.count("data-instance="))
        self.assertEqual([], [x.code for x in issues if x.code == "PREVIEW_TRUNCATED"])

    def test_R17_truncation_is_reported_after_the_drawn_count(self) -> None:
        path, profile = self.fixture("truncate")
        ir = extract_ir(path, profile)
        svg, issues = render_svg(ir, profile, mode="data", data_count=5, max_instances=3)
        self.assertEqual(3, svg.count('data-object-id="' + next(x.id for x in ir.elements if x.kind == "Record") + '" data-instance='))
        truncated = next(x for x in issues if x.code == "PREVIEW_TRUNCATED")
        self.assertIn("5件中3件", truncated.reason)

    def test_R17_two_display_settings_change_D_but_not_N(self) -> None:
        path, profile = self.fixture("d-and-n")
        for display, expected in ((3, 3), (7, 7)):
            with self.subTest(display=display):
                data = SAMPLE_XML.replace(b'displayLineCount="4"', f'displayLineCount="{display}"'.encode())
                ir = extract_ir(path, profile, data=data)
                record_id = next(x.id for x in ir.elements if x.kind == "Record")
                marker = f'data-object-id="{record_id}" data-instance='
                design, _ = render_svg(ir, profile, mode="design")
                rows, _ = render_svg(ir, profile, mode="data", data_count=2)
                self.assertEqual(expected, design.count(marker))
                self.assertEqual(2, rows.count(marker))

    def test_R15_change_sentence_uses_the_new_value(self) -> None:
        notes = AnnotationSet()
        notes.add(["record"], "表示行数を10行から12行に変更", "hash")
        changes, issues = notes.display_line_changes()
        self.assertEqual([], issues)
        self.assertEqual(12, changes[0]["value"])

    def test_R15_ambiguous_and_cancelled_notes_do_not_produce_changes(self) -> None:
        notes = AnnotationSet()
        notes.add(["record"], "表示行数は10行または20行", "hash")
        changes, issues = notes.display_line_changes()
        self.assertEqual([], changes)
        self.assertEqual(["COMMENT_AMBIGUOUS"], [x.code for x in issues])
        notes = AnnotationSet()
        note = notes.add(["record"], "表示行数を12行に変更", "hash")
        notes.cancel(note.id)
        self.assertEqual(([], []), notes.display_line_changes())

    def test_source_candidates_keep_unresolved_boxes_unresolved(self) -> None:
        document = {
            "unit": "mm",
            "objects": [
                {"id": "a", "page": 1, "text": "x", "bbox_source": "unresolved", "layout_bbox": {"x": 1, "y": 1}, "unresolved_reason": "幅と高さがありません"},
                {"id": "b", "page": 1, "text": "1", "bbox_source": "measured", "layout_bbox": {"x": 10, "y": 10, "width": 20, "height": 4}},
                {"id": "c", "page": 1, "text": "2", "bbox_source": "measured", "layout_bbox": {"x": 10, "y": 20, "width": 20, "height": 4}},
                {"id": "d", "page": 1, "text": "3", "bbox_source": "measured", "layout_bbox": {"x": 10, "y": 30, "width": 20, "height": 4}},
            ],
        }
        value, issues = build_candidates(document)
        self.assertEqual(["a"], [x["source_id"] for x in value["unresolved_objects"]])
        self.assertEqual(1, len(value["record_templates"]))
        self.assertAlmostEqual(10.0, value["record_templates"][0]["row_pitch_mm"])
        self.assertIn("SOURCE_BBOX_UNRESOLVED", [x.code for x in issues])


if __name__ == "__main__":
    unittest.main()
