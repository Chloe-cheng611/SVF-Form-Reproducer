"""最小の操作経路。

原稿読込、対象選択、備考の追加・修正・取消、表示行数の解決、LLM提案、検査、生成、
参考表示までを、ファイルの手修正を挟まずに同じserviceの作業単位で実行する。
"""
from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import load_profile, read_json
from .service import (
    apply_and_generate,
    generate_form,
    list_targets,
    load_annotations,
    propose_changes,
    render_reference,
    resolve_changes,
    save_annotations,
)
from .validation import structural_report
from .xmlcore import extract_ir

TARGET_KINDS = {"Record", "Field", "Text", "Line", "Box", "SubForm", "Bitmap"}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SVF XML 再現ツール")
        self.geometry("1024x720")
        self.xml_path = tk.StringVar()
        self.profile_path = tk.StringVar(value="config/profile.example.json")
        self.workspace = tk.StringVar()
        self.annotations_path = tk.StringVar()
        self.note_text = tk.StringVar()
        self.data_count = tk.StringVar(value="0")
        self.status = tk.StringVar(value="XML、profile、備考ファイルを選んで読込してください")
        self.source_hash = ""
        self.proposal: dict | None = None
        self._build()

    # --- 画面 ---------------------------------------------------------
    def _build(self) -> None:
        self._row("原稿XML", self.xml_path, self._choose_xml, 0)
        self._row("Profile", self.profile_path, self._choose_profile, 1)
        self._row("世代ワークスペース", self.workspace, self._choose_workspace, 2)
        self._row("備考ファイル", self.annotations_path, self._choose_annotations, 3)
        ttk.Button(self, text="読込", command=self.load).grid(row=4, column=0, padx=8, pady=6, sticky="w")
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=8)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        self.targets = ttk.Treeview(left, columns=("kind", "name", "display"), show="headings", selectmode="extended", height=12)
        for key, label, width in (("kind", "種別", 90), ("name", "名前", 160), ("display", "表示行数", 80)):
            self.targets.heading(key, text=label)
            self.targets.column(key, width=width, anchor="w")
        self.targets.pack(fill="both", expand=True)
        self.notes = ttk.Treeview(right, columns=("state", "text"), show="headings", height=12)
        for key, label, width in (("state", "状態", 90), ("text", "備考", 320)):
            self.notes.heading(key, text=label)
            self.notes.column(key, width=width, anchor="w")
        self.notes.pack(fill="both", expand=True)
        entry = ttk.Frame(self)
        entry.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=4)
        ttk.Label(entry, text="備考").pack(side="left")
        ttk.Entry(entry, textvariable=self.note_text, width=70).pack(side="left", padx=6)
        ttk.Button(entry, text="選択対象へ追加", command=self.add_note).pack(side="left", padx=2)
        ttk.Button(entry, text="修正", command=self.revise_note).pack(side="left", padx=2)
        ttk.Button(entry, text="取消", command=self.cancel_note).pack(side="left", padx=2)
        actions = ttk.Frame(self)
        actions.grid(row=7, column=0, columnspan=3, sticky="w", padx=8, pady=6)
        ttk.Button(actions, text="備考から解決", command=self.resolve).pack(side="left", padx=3)
        ttk.Button(actions, text="LLM提案", command=self.propose).pack(side="left", padx=3)
        ttk.Button(actions, text="検査", command=self.validate).pack(side="left", padx=3)
        ttk.Button(actions, text="生成", command=self.generate).pack(side="left", padx=3)
        ttk.Label(actions, text="件数N").pack(side="left", padx=(16, 2))
        ttk.Entry(actions, textvariable=self.data_count, width=8).pack(side="left")
        ttk.Button(actions, text="設計参考SVG", command=lambda: self.preview("design")).pack(side="left", padx=3)
        ttk.Button(actions, text="データ参考SVG", command=lambda: self.preview("data")).pack(side="left", padx=3)
        ttk.Label(self, textvariable=self.status, wraplength=980).grid(row=8, column=0, columnspan=3, sticky="w", padx=8)
        self.details = tk.Text(self, height=12)
        self.details.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=2)
        self.rowconfigure(9, weight=1)

    def _row(self, label: str, variable: tk.StringVar, command, row: int) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(self, text="選択", command=command).grid(row=row, column=2, padx=8)

    def _choose_xml(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("SVF XML", "*.xml"), ("All", "*")])
        if value:
            self.xml_path.set(value)

    def _choose_profile(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if value:
            self.profile_path.set(value)

    def _choose_workspace(self) -> None:
        value = filedialog.askdirectory()
        if value:
            self.workspace.set(value)

    def _choose_annotations(self) -> None:
        value = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")], confirmoverwrite=False)
        if value:
            self.annotations_path.set(value)

    def _show(self, value) -> None:
        self.details.delete("1.0", tk.END)
        self.details.insert(tk.END, json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def _guard(self, title: str, action):
        try:
            return action()
        except Exception as exc:  # 画面では例外を利用者向けに戻す
            messagebox.showerror(title, str(exc))
            self.status.set(f"{title}: {exc}")
            return None

    # --- 作業単位 -----------------------------------------------------
    def load(self) -> None:
        def action():
            ir = extract_ir(self.xml_path.get(), load_profile(self.profile_path.get()))
            self.source_hash = ir.source_hash
            self.proposal = None
            self.targets.delete(*self.targets.get_children())
            for item in list_targets(self.xml_path.get(), self.profile_path.get(), TARGET_KINDS):
                self.targets.insert("", "end", iid=item["id"], values=(item["kind"], item["name"] or "", item["display_line_count"] or ""))
            self._refresh_notes()
            self._show([x.to_dict() for x in ir.issues])
            self.status.set(f"要素{len(self.targets.get_children())}件。原本ハッシュ {ir.source_hash[:12]}。表示インスタンスではなく元要素IDを編集します。")

        self._guard("読込エラー", action)

    def _annotation_file(self) -> Path:
        value = self.annotations_path.get()
        if not value:
            raise ValueError("備考ファイルを選択してください")
        return Path(value)

    def _refresh_notes(self) -> None:
        notes = load_annotations(self._annotation_file()) if self.annotations_path.get() else None
        self.notes.delete(*self.notes.get_children())
        if notes is None:
            return
        for note in notes.annotations:
            state = "有効" if note.active else "取消"
            self.notes.insert("", "end", iid=note.id, values=(f"{state} r{note.revision}", note.text))

    def add_note(self) -> None:
        def action():
            targets = list(self.targets.selection())
            if not targets:
                raise ValueError("備考の対象を選択してください")
            if not self.note_text.get().strip():
                raise ValueError("備考の本文を入力してください")
            path = self._annotation_file()
            notes = load_annotations(path)
            note = notes.add(targets, self.note_text.get(), self.source_hash)
            save_annotations(path, notes)
            self._refresh_notes()
            self.status.set(f"備考を{len(targets)}件の対象へ追加しました: {note.id}")

        self._guard("備考エラー", action)

    def revise_note(self) -> None:
        def action():
            selected = self.notes.selection()
            if not selected:
                raise ValueError("修正する備考を選択してください")
            path = self._annotation_file()
            notes = load_annotations(path)
            note = notes.revise(selected[0], self.note_text.get(), self.source_hash, list(self.targets.selection()) or None)
            save_annotations(path, notes)
            self._refresh_notes()
            self.status.set(f"備考を版{note.revision}へ更新しました")

        self._guard("備考エラー", action)

    def cancel_note(self) -> None:
        def action():
            selected = self.notes.selection()
            if not selected:
                raise ValueError("取り消す備考を選択してください")
            path = self._annotation_file()
            notes = load_annotations(path)
            notes.cancel(selected[0])
            save_annotations(path, notes)
            self._refresh_notes()
            self.proposal = None
            self.status.set("備考を取り消しました。提案は破棄しました")

        self._guard("備考エラー", action)

    def resolve(self) -> None:
        def action():
            notes = load_annotations(self._annotation_file())
            changes, issues = resolve_changes(self.xml_path.get(), self.profile_path.get(), notes)
            self._show({"changes": changes, "issues": [x.to_dict() for x in issues]})
            self.status.set(f"備考から{len(changes)}件の変更を解決しました。error {sum(1 for x in issues if x.severity == 'error')}件")

        self._guard("解決エラー", action)

    def propose(self) -> None:
        def action():
            notes = load_annotations(self._annotation_file())
            index_path = filedialog.askopenfilename(title="few-shot索引(任意)", filetypes=[("JSON", "*.json")])
            index = read_json(index_path) if index_path else None
            self.proposal = propose_changes(self.xml_path.get(), self.profile_path.get(), notes, fewshot_index=index, target_ids=list(self.targets.selection()) or None)
            self._show(self.proposal)
            self.status.set(f"提案を受け取りました。変更{len(self.proposal.get('changes', []))}件。生成時に対象範囲と版を再検査します")

        self._guard("提案エラー", action)

    def validate(self) -> None:
        def action():
            report = structural_report(self.xml_path.get(), load_profile(self.profile_path.get()))
            self._show(report)
            self.status.set("構造検査: " + report["checks"][0]["status"])

        self._guard("検査エラー", action)

    def generate(self) -> None:
        def action():
            output = filedialog.asksaveasfilename(defaultextension=".xml", filetypes=[("SVF XML", "*.xml")])
            if not output:
                return
            notes = load_annotations(self._annotation_file())
            workspace = self.workspace.get() or None
            if self.proposal is not None:
                report = apply_and_generate(self.xml_path.get(), self.profile_path.get(), self.proposal, notes, output, workspace=workspace)
            else:
                changes, issues = resolve_changes(self.xml_path.get(), self.profile_path.get(), notes)
                if any(x.severity == "error" for x in issues):
                    self._show([x.to_dict() for x in issues])
                    raise ValueError("備考の解決にerrorがあります")
                if not changes:
                    raise ValueError("適用する変更がありません")
                report = generate_form(self.xml_path.get(), self.profile_path.get(), changes, output, workspace=workspace)
            self._show(report)
            state = "確定" if report.get("committed") else "未確定"
            self.status.set(f"構造検査 {report['checks'][0]['status']}／世代 {state}／出力 {report.get('output_written')}")

        self._guard("生成エラー", action)

    def preview(self, mode: str) -> None:
        def action():
            output = filedialog.asksaveasfilename(defaultextension=".svg", filetypes=[("SVG", "*.svg")])
            if not output:
                return
            count = None
            if mode == "data":
                text = self.data_count.get().strip()
                if not text.isdigit():
                    raise ValueError("件数Nは0以上の整数で指定してください")
                count = int(text)
            issues = render_reference(self.xml_path.get(), self.profile_path.get(), output, mode=mode, data_count=count)
            self._show([x.to_dict() for x in issues])
            self.status.set(f"参考SVGを保存しました: {output}")

        self._guard("参考表示エラー", action)


def main() -> None:
    App().mainloop()
