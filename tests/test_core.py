from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from svf_reproducer.annotations import AnnotationSet
from svf_reproducer.config import stable_json_hash, write_json
from svf_reproducer.experiments import create_comparison_plan
from svf_reproducer.fewshot import build_index, select_examples
from svf_reproducer.geometry import Affine, dot_to_mm, mm_to_dot, pdf_page_to_top_left_mm, point_to_mm, px_to_mm
from svf_reproducer.jobs import run_runtime, validate_job
from svf_reproducer.models import Job, MappingConfig, MappingField, RuntimeResult, TargetProfile
from svf_reproducer.preview import render_svg
from svf_reproducer.proposals import apply_proposal
from svf_reproducer.storage import JobLedger, load_current, save_revision
from svf_reproducer.service import generate_form
from svf_reproducer.validation import create_evidence_manifest, record_check, structural_report
from svf_reproducer.xmlcore import CompilationError, compile_xml, estimate_capacity, extract_ir, parse_xml, resolve_display_line_count, validate_xml


SAMPLE_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<FormData version="9.2" designer="9.1">
  <Paper freeWidth="210" freeHeight="297" resolution="400" />
  <Field name="HEADER" x="100" y="100" charCount="10" vendorAttribute="keep" />
  <Line name="H" x1="100" y1="200" x2="500" y2="200" />
  <SubForm name="DETAIL" linkName="OVERFLOW" x1="100" y1="400" x2="1000" y2="800" direction="1">
    <Record name="R1" x1="100" y1="400" x2="1000" y2="500" heightUnit="0" height="100" displayLineCount="4">
      <Field name="CODE" x="120" y="420" charCount="8" strCalcFormula="" />
      <VendorThing name="opaque" custom="untouched"><Child value="1" /></VendorThing>
    </Record>
  </SubForm>
  <SubForm name="OVERFLOW" linkName="" x1="100" y1="900" x2="1000" y2="1300" direction="1" />
</FormData>
'''


def make_profile(xml_path: Path) -> TargetProfile:
    return TargetProfile(
        "test",
        product="SVF",
        version="test-version",
        output_device="PDF",
        host_os="test-os",
        coordinate_dpi=400,
        base_xml=str(xml_path),
        compatibility={"Record": "edit_supported", "Field": "preserve_only", "SubForm": "preserve_only"},
        editable_attributes={"Record": ["displayLineCount"]},
        enum_maps={"subform.direction": {"1": "vertical"}},
    )


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.xml = self.root / "form.xml"
        self.xml.write_bytes(SAMPLE_XML)
        self.profile = make_profile(self.xml)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ir_keeps_display_count_parent_and_unknown_node(self) -> None:
        ir = extract_ir(self.xml, self.profile)
        record = next(x for x in ir.elements if x.kind == "Record")
        field = next(x for x in ir.elements if x.name == "CODE")
        self.assertEqual(4, record.display_line_count)
        self.assertEqual(record.id, field.parent_id)
        self.assertTrue(any(x.kind == "VendorThing" for x in ir.elements))

    def test_named_stable_id_survives_unrelated_sibling_insertion(self) -> None:
        before = next(x.id for x in extract_ir(self.xml, self.profile).elements if x.name == "R1")
        changed = self.root / "with-sibling.xml"
        changed.write_bytes(SAMPLE_XML.replace(b'<SubForm name="DETAIL"', b'<Text name="NEW" x="1" y="1" strText="new" />\n  <SubForm name="DETAIL"'))
        after = next(x.id for x in extract_ir(changed, self.profile).elements if x.name == "R1")
        self.assertEqual(before, after)

    def test_no_change_returns_original_bytes(self) -> None:
        self.assertEqual(SAMPLE_XML, compile_xml(self.xml, [], self.profile))

    def test_allowed_change_preserves_unknown_semantics(self) -> None:
        ir = extract_ir(self.xml, self.profile)
        record = next(x for x in ir.elements if x.kind == "Record")
        body = compile_xml(self.xml, [{"target_id": record.id, "op": "set", "property": "displayLineCount", "value": 9}], self.profile)
        root = parse_xml(body)
        changed = root.find(".//Record")
        opaque = root.find(".//VendorThing")
        self.assertEqual("9", changed.attrib["displayLineCount"])
        self.assertEqual("untouched", opaque.attrib["custom"])
        self.assertEqual("1", opaque.find("Child").attrib["value"])

    def test_disallowed_change_is_atomic(self) -> None:
        record = next(x for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        with self.assertRaises(CompilationError):
            compile_xml(self.xml, [{"target_id": record.id, "op": "set", "property": "name", "value": "BAD"}], self.profile)
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())

    def test_add_uses_confirmed_profile_template_only(self) -> None:
        profile = make_profile(self.xml)
        profile.compatibility["Line"] = "edit_supported"
        profile.editable_attributes["Line"] = ["name", "x1", "y1", "x2", "y2"]
        profile.element_templates["detail-rule"] = '<Line name="RULE_TEMPLATE" x1="0" y1="0" x2="100" y2="0" lineType="0" />'
        root_id = next(x.id for x in extract_ir(self.xml, profile).elements if x.kind == "FormData")
        body = compile_xml(self.xml, [{"target_id": root_id, "op": "add", "value": {"template": "detail-rule", "attributes": {"name": "RULE_1", "y1": 700, "y2": 700}}}], profile)
        line = next(x for x in parse_xml(body).findall("Line") if x.attrib.get("name") == "RULE_1")
        self.assertEqual("0", line.attrib["lineType"])
        with self.assertRaises(CompilationError):
            compile_xml(self.xml, [{"target_id": root_id, "op": "add", "value": {"template": "model-invented"}}], profile)

    def test_geometry_accepts_horizontal_line_and_empty_link_target(self) -> None:
        errors = [x for x in validate_xml(self.xml, self.profile) if x.severity == "error"]
        self.assertEqual([], errors)

    def test_zero_length_line_is_rejected(self) -> None:
        broken = self.root / "broken.xml"
        broken.write_bytes(SAMPLE_XML.replace(b'x2="500"', b'x2="100"'))
        self.assertIn("GEOMETRY_INVALID", {x.code for x in validate_xml(broken, self.profile)})

    def test_display_count_priority_and_capacity(self) -> None:
        self.assertEqual((8, "annotation"), resolve_display_line_count(8, 4, 3, 2))
        self.assertEqual((4, "original_xml"), resolve_display_line_count(None, 4, 3, 2))
        self.assertEqual((4, None), estimate_capacity(20, 5, direction="vertical"))
        self.assertEqual((None, "direction"), estimate_capacity(20, 5, direction="horizontal"))

    def test_coordinate_units_and_rotation_are_reversible(self) -> None:
        self.assertAlmostEqual(25.4, px_to_mm(300, 300))
        self.assertAlmostEqual(25.4, point_to_mm(72))
        self.assertAlmostEqual(400, mm_to_dot(25.4, 400))
        self.assertAlmostEqual(25.4, dot_to_mm(400, 400))
        transform = pdf_page_to_top_left_mm(595, 842, rotation=90)
        point = transform.apply(123.0, 456.0)
        restored = transform.inverse().apply(*point)
        self.assertAlmostEqual(123.0, restored[0])
        self.assertAlmostEqual(456.0, restored[1])

    def test_design_preview_uses_D_and_data_preview_uses_N(self) -> None:
        ir = extract_ir(self.xml, self.profile)
        record_id = next(x.id for x in ir.elements if x.kind == "Record")
        design, _ = render_svg(ir, self.profile, mode="design")
        data, _ = render_svg(ir, self.profile, mode="data", data_count=2)
        self.assertEqual(4, design.count(f'data-object-id="{record_id}" data-instance='))
        self.assertEqual(2, data.count(f'data-object-id="{record_id}" data-instance='))
        self.assertEqual(4, next(x.display_line_count for x in ir.elements if x.id == record_id))

    def test_annotations_detect_conflict_and_cancel_rebuilds(self) -> None:
        record_id = next(x.id for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        notes = AnnotationSet()
        first = notes.add([record_id], "表示を6行", "hash")
        notes.add([record_id], "表示行数: 7行", "hash")
        changes, issues = notes.display_line_changes()
        self.assertEqual([], changes)
        self.assertEqual("COMMENT_CONFLICT", issues[0].code)
        notes.cancel(first.id)
        changes, issues = notes.display_line_changes()
        self.assertEqual(7, changes[0]["value"])
        self.assertEqual([], issues)

    def test_proposal_rejects_stale_and_applies_current(self) -> None:
        ir = extract_ir(self.xml, self.profile)
        record = next(x for x in ir.elements if x.kind == "Record")
        notes = AnnotationSet()
        note = notes.add([record.id], "表示を5行", ir.source_hash)
        proposal = {
            "source_hash": ir.source_hash,
            "base_revision": ir.revision,
            "annotation_revisions": {note.id: 1},
            "changes": [{"target_id": record.id, "op": "set", "property": "displayLineCount", "value": 5, "note_ids": [note.id], "example_ids": []}],
            "xml_fragments": [],
            "unresolved": [],
        }
        body = apply_proposal(self.xml, proposal, source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=self.profile)
        self.assertEqual("5", parse_xml(body).find(".//Record").attrib["displayLineCount"])
        proposal["base_revision"] = 0
        with self.assertRaises(CompilationError):
            apply_proposal(self.xml, proposal, source_hash=ir.source_hash, revision=ir.revision, annotations=notes, profile=self.profile)

    def test_zero_data_keeps_header_and_leading_zero(self) -> None:
        csv_path = self.root / "detail.csv"
        csv_path.write_text("code\r\n", encoding="utf-8", newline="")
        mapping = MappingConfig("m1", [
            MappingField("title", "header", "HEADER", required=True, null_policy="error"),
            MappingField("code", "detail", "CODE", record_id="R1"),
        ])
        job = Job("j1", "r1", stable_json_hash(mapping.to_dict()), {"title": "見出し"}, str(csv_path), 0, "test", "skip")
        rows, issues = validate_job(job, mapping, self.profile)
        self.assertEqual([], rows)
        self.assertEqual([], [x for x in issues if x.severity == "error"])
        csv_path.write_text("code\r\n0012\r\n", encoding="utf-8", newline="")
        job.data_count = 1
        rows, issues = validate_job(job, mapping, self.profile)
        self.assertEqual("0012", rows[0]["code"])

    def test_common_csv_rejects_lf_only(self) -> None:
        csv_path = self.root / "detail.csv"
        csv_path.write_bytes(b"code\n0012\n")
        mapping = MappingConfig("m1", [MappingField("code", "detail", "CODE", record_id="R1")])
        job = Job("j1", "r1", stable_json_hash(mapping.to_dict()), {}, str(csv_path), 1, "test", "skip")
        _, issues = validate_job(job, mapping, self.profile)
        self.assertIn("CSV_LINE_ENDING_INVALID", {x.code for x in issues})

    def test_generation_save_and_incomplete_revision(self) -> None:
        first = save_revision(self.root / "work", "r1", {"form.xml": SAMPLE_XML, "ir.json": {"revision": 1}})
        (self.root / "work" / "revisions" / "r2.tmp").mkdir()
        self.assertEqual(first, load_current(self.root / "work"))

    def test_load_current_rejects_corrupt_artifact(self) -> None:
        current = save_revision(self.root / "work-corrupt", "r1", {"form.xml": SAMPLE_XML})
        (current / "form.xml").write_bytes(b"corrupt")
        with self.assertRaises(OSError):
            load_current(self.root / "work-corrupt")

    def test_revision_rejects_path_escape(self) -> None:
        with self.assertRaises(ValueError):
            save_revision(self.root / "work-escape", "../outside", {"form.xml": SAMPLE_XML})
        with self.assertRaises(ValueError):
            save_revision(self.root / "work-escape", "r1", {"../../outside.xml": SAMPLE_XML})

    def test_generate_can_commit_a_complete_revision(self) -> None:
        profile_path = self.root / "profile.json"
        write_json(profile_path, self.profile.to_dict())
        record = next(x for x in extract_ir(self.xml, self.profile).elements if x.kind == "Record")
        output = self.root / "generated.xml"
        report = generate_form(
            self.xml,
            profile_path,
            [{"target_id": record.id, "op": "set", "property": "displayLineCount", "value": 6}],
            output,
            workspace=self.root / "work-generated",
            revision="r002",
        )
        current = load_current(self.root / "work-generated")
        self.assertEqual("r002", current.name)
        self.assertEqual("6", ET.parse(current / "generated.xml").find(".//Record").attrib["displayLineCount"])
        self.assertEqual(str(current), report["revision_path"])

    def test_ledger_blocks_conflicting_and_unknown_replay(self) -> None:
        ledger = JobLedger(self.root / "work")
        unknown = RuntimeResult("UNKNOWN", "runtime-1", 2, error_code="RUNTIME_TIMEOUT")
        ledger.record("job-1", "hash-a", unknown)
        result = ledger.before_run("job-1", "hash-a")
        self.assertEqual("JOB_RESULT_UNKNOWN", result[0].code)
        result = ledger.before_run("job-1", "hash-b")
        self.assertEqual("JOB_ID_CONFLICT", result[0].code)

    def test_ledger_claim_blocks_parallel_run(self) -> None:
        ledger = JobLedger(self.root / "work")
        self.assertIsNone(ledger.claim("job-1", "hash-a"))
        result = ledger.claim("job-1", "hash-a")
        self.assertEqual("JOB_ALREADY_RUNNING", result[0].code)

    def test_runtime_does_not_accept_old_artifact(self) -> None:
        output = self.root / "output"
        output.mkdir()
        (output / "old.pdf").write_bytes(b"old")
        csv_path = self.root / "detail.csv"
        csv_path.write_bytes(b"code\r\n0012\r\n")
        mapping = MappingConfig("m1", [MappingField("code", "detail", "CODE", record_id="R1")])
        job = Job("j1", "r1", stable_json_hash(mapping.to_dict()), {}, str(csv_path), 1, "test", "skip")
        profile = make_profile(self.xml)
        profile.runtime_kind = "command"
        profile.runtime_command = [sys.executable, "-c", "print('done')"]
        result = run_runtime(self.xml, job, mapping, profile, output)
        self.assertEqual("FAILED", result.status)
        self.assertEqual("RUNTIME_ARTIFACT_MISSING", result.error_code)

    def test_runtime_sanitizes_job_directory_and_rejects_escaping_glob(self) -> None:
        csv_path = self.root / "detail.csv"
        csv_path.write_bytes(b"code\r\n0012\r\n")
        mapping = MappingConfig("m1", [MappingField("code", "detail", "CODE", record_id="R1")])
        job = Job("../../bad", "r1", stable_json_hash(mapping.to_dict()), {}, str(csv_path), 1, "test", "skip")
        profile = make_profile(self.xml)
        profile.runtime_kind = "command"
        profile.runtime_command = [sys.executable, "-c", "print('done')"]
        profile.runtime_artifact_globs = ["../*.pdf"]
        result = run_runtime(self.xml, job, mapping, profile, self.root / "output-safe")
        self.assertEqual("PROFILE_INVALID", result.error_code)
        self.assertFalse((self.root / "bad").exists())

    def test_comparison_plan_keeps_D_N_and_geometry_independent(self) -> None:
        plan = create_comparison_plan(4)
        by_id = {x["case_id"]: x for x in plan["cases"]}
        self.assertEqual(["display_line_count"], by_id["D-only"]["change"])
        self.assertEqual(["data_count"], by_id["N-only"]["change"])
        self.assertEqual([0, 1, 2, 3, 4, 5, 9], by_id["N-only"]["values"])
        self.assertTrue(all(x["status"] == "not_run" for x in plan["cases"]))

    def test_fewshot_index_is_reference_metadata(self) -> None:
        index = build_index(self.xml)
        records = select_examples(index, ["Record"])
        self.assertEqual(1, len(records))
        self.assertIn("attribute_names", records[0])
        self.assertNotIn("apply", records[0])

    def test_validation_cannot_pass_without_evidence(self) -> None:
        report_path = self.root / "validation.json"
        write_json(report_path, structural_report(self.xml, self.profile))
        with self.assertRaises(ValueError):
            record_check(report_path, "designer", "pass", [])
        evidence = self.root / "designer-result.txt"
        evidence.write_text("opened and saved", encoding="utf-8")
        with self.assertRaises(ValueError):
            record_check(report_path, "designer", "pass", [evidence])
        manifest_path = self.root / "designer-evidence.json"
        create_evidence_manifest(
            report_path,
            "designer",
            [evidence],
            manifest_path,
            "対象Designerで開閉・保存",
            metadata={"product": "SVFX-Designer", "version": "test-version", "operator": "受入担当", "result": "開いて保存できた"},
        )
        report = record_check(report_path, "designer", "pass", [manifest_path])
        designer = next(x for x in report["checks"] if x["kind"] == "designer")
        self.assertEqual("pass", designer["status"])
        self.assertEqual(64, len(designer["evidence"][0]["sha256"]))

    def test_preview_evidence_rejects_different_source_hash(self) -> None:
        report_path = self.root / "validation.json"
        write_json(report_path, structural_report(self.xml, self.profile))
        wrong_svg = self.root / "wrong.svg"
        wrong_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" data-source-hash="wrong" data-profile-hash="wrong"/>', encoding="utf-8")
        with self.assertRaises(ValueError):
            create_evidence_manifest(report_path, "preview", [wrong_svg], self.root / "evidence.json")


if __name__ == "__main__":
    unittest.main()
