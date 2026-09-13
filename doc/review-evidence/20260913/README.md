# 2026年9月13日 実装レビューの証拠

製品コードを修正する前の観察記録。修正後の期待結果はWordレポート各章の受入条件を参照する。

- `reproduce.py`: 20ケースの最小再現。元のsrcとtestsを読み、入力や台帳・出力は毎回新しい一時領域に作る。実SVFや印刷は呼ばない。例外も観察結果として保存するため、このスクリプトの終了コード0は機能の合格を意味しない。
- `results.json`: 今回の実行結果。再実行ではUUIDと一時パス由来のハッシュは変わる。確認するのは各ケースの動作。
- `baseline.json`: 調査開始時の製品Python、既存試験、既存Word、few-shot、README、実装状況のSHA-256。

実行環境はmacOS／Python 3.13.5。既存27試験は成功。WindowsのR02はfcntlの不在を模擬したimport試験で、Windows実機試験ではない。現行srcはfcntlに依存するため、この再現スクリプト自体も現状はPOSIX環境で実行する。

```bash
python3 doc/review-evidence/20260913/reproduce.py
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

再現スクリプトは最後に新しい証拠ディレクトリを表示する。プロジェクト内の元XML・CSV・既存台帳には書き込まない。

R10はSubFormの幅を負にする例で検査失敗後の上書きを確認した。Lineの端点順の反転は必ずしも不正ではないので、このケースの根拠にしない。R12は版リストが空のテストprofileを含む。R14は小規模な内部実体の展開だけを調べた。R08は入力ハッシュと台帳claimの組合せによる旧結果再利用を確認した。R19はローカルの模擬実行器が空PDFを作る。

fsync、OCR timeout、PDF bbox、未接続関数はコード照合。停電、Windows実機、Designer開閉、実SVFの改ページ・印字と外部ジョブ照会は未実施。
