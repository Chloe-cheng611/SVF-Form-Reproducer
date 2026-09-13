"""通常のCLIとserviceを経由した統合試験と、件数・設定の回帰。

関数単体の存在ではなく、原稿読込から備考・解決・提案・生成・参考表示・実行・受入
記録までを通常の操作経路でつなぐ。実SVFとDesignerは呼ばない。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from svf_reproducer import gui, service
from svf_reproducer.cli import main
from svf_reproducer.config import load_profile, write_json
from svf_reproducer.experiments import create_comparison_plan
from svf_reproducer.layout import analyze_record_layouts
from svf_reproducer.preview import render_svg
from svf_reproducer.storage import load_current
from svf_reproducer.xmlcore import extract_ir

from test_core import SAMPLE_XML, make_profile

VALID_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def run(*argv: str) -> tuple[int, object]:
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = main(list(argv))
    text = stream.getvalue()
    return code, (json.loads(text) if text.strip().startswith(("{", "[")) else text.strip())


class CommandLinePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.xml = self.root / "form.xml"
        self.xml.write_bytes(SAMPLE_XML)
        profile = make_profile(self.xml)
        profile.enum_maps["record.heightUnit"] = {"0": "dot"}
        self.profile_path = self.root / "profile.json"
        write_json(self.profile_path, profile.to_dict())
        self.workspace = self.root / "workspace"
        self.notes = self.root / "annotations.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manuscript_to_generation_and_reference_without_hand_editing(self) -> None:
        code, imported = run("import", str(self.xml), str(self.profile_path), str(self.workspace), "--revision", "base")
        self.assertEqual(0, code, imported)
        code, targets = run("targets", str(self.xml), str(self.profile_path), "--kind", "Record")
        self.assertEqual(0, code)
        record_id = targets["targets"][0]["id"]
        source_hash = extract_ir(self.xml, load_profile(self.profile_path)).source_hash

        code, note = run("annotation-add", str(self.notes), source_hash, "表示行数を10行から12行に変更", record_id)
        self.assertEqual(0, code, note)
        code, resolved = run("resolve", str(self.xml), str(self.profile_path), str(self.notes), "--output", str(self.root / "changes.json"))
        self.assertEqual(0, code, resolved)
        self.assertEqual(12, resolved["changes"][0]["value"])

        output = self.root / "generated.xml"
        code, report = run(
            "generate", str(self.xml), str(self.profile_path), str(self.root / "changes.json"), str(output), "--workspace", str(self.workspace), "--revision", "r002"
        )
        self.assertEqual(0, code, report)
        self.assertTrue(report["committed"])
        self.assertTrue(report["output_written"])
        self.assertEqual("r002", load_current(self.workspace).name)
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())
        self.assertIn(b'displayLineCount="12"', output.read_bytes())

        code, validated = run("validate", str(output), str(self.profile_path), "--output", str(self.root / "validation.json"), "--form-revision", "r002")
        self.assertEqual(0, code, validated)
        marker = f'data-object-id="{record_id}" data-instance='
        code, design = run("render-test", str(output), str(self.profile_path), str(self.root / "design.svg"))
        self.assertEqual(0, code, design)
        self.assertEqual(12, (self.root / "design.svg").read_text().count(marker))
        code, data = run("render-test", str(output), str(self.profile_path), str(self.root / "data.svg"), "--mode", "data", "--data-count", "0")
        self.assertEqual(0, code, data)
        self.assertEqual(0, (self.root / "data.svg").read_text().count(marker))
        code, layout = run("layout", str(output), str(self.profile_path))
        self.assertEqual(0, code, layout)
        self.assertEqual("height(dot)", layout["layouts"][0]["pitch_source"])

        code, cancelled = run("annotation-cancel", str(self.notes), note["id"])
        self.assertEqual(0, code, cancelled)
        code, resolved = run("resolve", str(self.xml), str(self.profile_path), str(self.notes))
        self.assertEqual(0, code)
        self.assertEqual([], resolved["changes"])

    def test_proposal_path_applies_only_in_scope_changes(self) -> None:
        record_id = run("targets", str(self.xml), str(self.profile_path), "--kind", "Record")[1]["targets"][0]["id"]
        source_hash = extract_ir(self.xml, load_profile(self.profile_path)).source_hash
        run("annotation-add", str(self.notes), source_hash, "表示行数を9行に変更", record_id)
        stub = self.root / "stub_model.py"
        stub.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "note = sorted(payload['annotation_revisions'])[0]\n"
            "target = payload['targets'][0]['id']\n"
            "print(json.dumps({\n"
            "    'source_hash': payload['source_hash'],\n"
            "    'base_revision': payload['base_revision'],\n"
            "    'annotation_revisions': payload['annotation_revisions'],\n"
            "    'changes': [{'target_id': target, 'op': 'set', 'property': 'displayLineCount',\n"
            "                 'value': 9, 'note_ids': [note], 'example_ids': []}],\n"
            "    'xml_fragments': [], 'unresolved': []}))\n",
            encoding="utf-8",
        )
        profile = load_profile(self.profile_path)
        profile.proposal_command = [sys.executable, str(stub)]
        write_json(self.profile_path, profile.to_dict())
        proposal_path = self.root / "proposal.json"
        code, value = run("propose", str(self.xml), str(self.profile_path), str(self.notes), str(proposal_path), "--target-id", record_id)
        self.assertEqual(0, code, value)
        output = self.root / "proposed.xml"
        code, report = run(
            "proposal-apply", str(self.xml), str(self.profile_path), str(proposal_path), str(self.notes), str(output), "--workspace", str(self.workspace), "--revision", "p001"
        )
        self.assertEqual(0, code, report)
        self.assertIn(b'displayLineCount="9"', output.read_bytes())
        self.assertEqual([record_id], [x["target_id"] for x in report["applied_changes"]])

    def test_run_status_and_runtime_acceptance(self) -> None:
        record_id = run("targets", str(self.xml), str(self.profile_path), "--kind", "Record")[1]["targets"][0]["id"]
        write_json(self.root / "changes.json", {"changes": [{"op": "set", "target_id": record_id, "property": "displayLineCount", "value": 6}]})
        form = self.root / "generated.xml"
        run("generate", str(self.xml), str(self.profile_path), str(self.root / "changes.json"), str(form), "--workspace", str(self.workspace), "--revision", "r010")
        mapping_path = self.root / "mapping.json"
        write_json(mapping_path, {"mapping_id": "m1", "fields": [{"source_key": "code", "scope": "detail", "svf_field": "CODE", "record_id": "R1"}]})
        code, digest = run("mapping-hash", str(mapping_path))
        csv_path = self.root / "detail.csv"
        csv_path.write_bytes(b"code\r\n0012\r\n")
        job_path = self.root / "job.json"
        write_json(
            job_path,
            {
                "job_id": "job-1",
                "form_revision": "r010",
                "mapping_hash": digest,
                "header": {},
                "data_file": str(csv_path),
                "data_count": 1,
                "output_profile": "test",
                "empty_policy": "skip",
            },
        )
        profile = load_profile(self.profile_path)
        profile.runtime_kind = "command"
        profile.runtime_command = [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1], 'out.pdf').write_bytes(bytes.fromhex(sys.argv[2]))",
            "{output_dir}",
            VALID_PDF.hex(),
        ]
        write_json(self.profile_path, profile.to_dict())
        result_path = self.root / "runtime-result.json"
        code, result = run(
            "run", str(form), str(job_path), str(mapping_path), str(self.profile_path), str(self.workspace), str(self.root / "runtime-output"), "--result-output", str(result_path)
        )
        self.assertEqual(0, code, result)
        self.assertEqual("SUCCEEDED", result["status"])
        self.assertTrue(result["input_manifest_hash"])
        self.assertEqual(1, len(result["artifacts"]))

        code, status = run("status", str(self.workspace), "--job-id", "job-1")
        self.assertEqual(0, code)
        self.assertEqual("FINISHED", status["jobs"]["job-1"]["state"])

        code, opened = run("acceptance-open", str(self.workspace), "r010")
        self.assertEqual(0, code, opened)
        report_path = Path(opened["report"])
        self.assertTrue(str(report_path).endswith(str(Path("acceptance") / "r010" / "validation.json")))
        evidence_path = self.root / "runtime-evidence.json"
        code, manifest = run(
            "evidence-create", str(report_path), "runtime", str(evidence_path), result["artifact_paths"][0], "--runtime-result", str(result_path), "--note", "模擬実行器"
        )
        self.assertEqual(0, code, manifest)
        code, recorded = run("record-check", str(report_path), "runtime", "pass", str(evidence_path))
        self.assertEqual(0, code, recorded)
        runtime_check = next(x for x in recorded["checks"] if x["kind"] == "runtime")
        self.assertEqual("pass", runtime_check["status"])
        self.assertEqual("r010", load_current(self.workspace).name, "受入記録の登録で確定世代を壊さない")

        csv_path.write_bytes(b"code\r\n9999\r\n")
        code, conflicted = run("run", str(form), str(job_path), str(mapping_path), str(self.profile_path), str(self.workspace), str(self.root / "runtime-output-2"))
        self.assertEqual(4, code)
        self.assertEqual("JOB_ID_CONFLICT", conflicted["error_code"])

    def test_generate_refuses_to_overwrite_the_source(self) -> None:
        write_json(self.root / "changes.json", {"changes": []})
        code, value = run("generate", str(self.xml), str(self.profile_path), str(self.root / "changes.json"), str(self.xml))
        self.assertEqual(2, code)
        self.assertEqual("OUTPUT_PATH_CONFLICT", value["issues"][0]["code"])
        self.assertEqual(SAMPLE_XML, self.xml.read_bytes())

    def test_gui_uses_the_same_service_work_units(self) -> None:
        expected = {
            "list_targets",
            "load_annotations",
            "save_annotations",
            "resolve_changes",
            "propose_changes",
            "generate_form",
            "apply_and_generate",
            "render_reference",
        }
        for name in expected:
            self.assertIs(getattr(gui, name), getattr(service, name), f"GUIは{name}をserviceから使う")
        for method in ("load", "add_note", "revise_note", "cancel_note", "resolve", "propose", "validate", "generate", "preview"):
            self.assertTrue(callable(getattr(gui.App, method)), method)


class CountAndSettingTests(unittest.TestCase):
    """異なる領域高と行高の2設定で、N=0、1、2、C-1、C、C+1、2C+1とDを確認する。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, subform_bottom: int, row_height: int):
        data = SAMPLE_XML.replace(b'linkName="OVERFLOW"', b'linkName=""')
        data = data.replace(b'x1="100" y1="400" x2="1000" y2="800"', f'x1="100" y1="400" x2="1000" y2="{subform_bottom}"'.encode())
        data = data.replace(b'heightUnit="0" height="100"', f'heightUnit="0" height="{row_height}"'.encode())
        path = self.root / f"form-{subform_bottom}-{row_height}.xml"
        path.write_bytes(data)
        profile = make_profile(path)
        profile.enum_maps["record.heightUnit"] = {"0": "dot"}
        return path, profile

    def test_boundary_counts_for_two_geometry_settings(self) -> None:
        for bottom, height, expected_capacity in ((800, 100, 4), (1200, 80, 10)):
            with self.subTest(subform_bottom=bottom, row_height=height):
                path, profile = self.build(bottom, height)
                ir = extract_ir(path, profile)
                layouts, issues = analyze_record_layouts(ir, profile)
                self.assertEqual([], [x.code for x in issues if x.severity == "error"])
                capacity = layouts[0].capacity
                self.assertEqual(expected_capacity, capacity)
                record_id = next(x.id for x in ir.elements if x.kind == "Record")
                marker = f'data-object-id="{record_id}" data-instance='
                for count in sorted({0, 1, 2, capacity - 1, capacity, capacity + 1, 2 * capacity + 1}):
                    svg, preview_issues = render_svg(ir, profile, mode="data", data_count=count, layouts=layouts)
                    self.assertEqual(count, svg.count(marker), f"N={count}")
                    self.assertEqual([], [x.code for x in preview_issues if x.severity == "error"])
                for display in (3, 7):
                    changed = path.read_bytes().replace(b'displayLineCount="4"', f'displayLineCount="{display}"'.encode())
                    design_ir = extract_ir(path, profile, data=changed)
                    design, _ = render_svg(design_ir, profile, mode="design", layouts=layouts)
                    self.assertEqual(display, design.count(marker))
                plan = create_comparison_plan(capacity)
                values = next(x for x in plan["cases"] if x["case_id"] == "N-only")["values"]
                self.assertEqual(sorted({0, 1, 2, capacity - 1, capacity, capacity + 1, 2 * capacity + 1}), values)
                self.assertTrue(all(x["status"] == "not_run" for x in plan["cases"]), "実機の期待値は未実施のままにする")

    def test_capacity_is_unconfirmed_without_a_registered_height_unit(self) -> None:
        path, profile = self.build(800, 100)
        profile.enum_maps.pop("record.heightUnit")
        layouts, issues = analyze_record_layouts(extract_ir(path, profile), profile)
        self.assertIsNone(layouts[0].capacity)
        self.assertEqual("pitch_unconfirmed", layouts[0].capacity_reason)
        self.assertIn("PITCH_UNCONFIRMED", [x.code for x in issues])


if __name__ == "__main__":
    unittest.main()
