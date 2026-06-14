from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT_PATH = ROOT / "scripts" / "migrate_za_bank_to_v2.py"
EXPORT_SCRIPT_PATH = ROOT / "scripts" / "export_za_unified_audit.py"
REVIEW_SCRIPT_PATH = ROOT / "scripts" / "render_za_medium_review_report.py"
PROJECT_DIR = ROOT / "2026-05-05_za_bank_sample_01"
SCHEMA_PATH = ROOT / "schema-v2-draft.sql"


def load_module(name: str, path: Path):
    assert path.exists(), f"module missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_render_za_medium_review_report_creates_expected_markdown(tmp_path):
    migrate_module = load_module("migrate_za_bank_to_v2_for_review_report", MIGRATE_SCRIPT_PATH)
    export_module = load_module("export_za_bank_for_review_report", EXPORT_SCRIPT_PATH)
    review_module = load_module("render_za_medium_review_report", REVIEW_SCRIPT_PATH)

    db_path = tmp_path / "za_transactions_v2.sqlite"
    migrate_module.migrate_project(
        project_dir=PROJECT_DIR,
        schema_path=SCHEMA_PATH,
        output_path=db_path,
        overwrite=True,
    )

    export_result = export_module.save_za_unified_audit_export(
        db_path=db_path,
        output_dir=tmp_path / "exports",
        timestamp_label="2026-05-18_22-20-00",
        generated_at_label="2026-05-18_22-20-00 CST",
    )

    report_path = review_module.save_za_medium_confidence_review_report(
        csv_path=export_result["csv_path"],
        output_dir=tmp_path / "reports",
        timestamp_label="2026-05-18_22-21-00",
        generated_at_label="2026-05-18 22:21:00 CST",
    )

    assert report_path.name == "za_medium_confidence_review_report_2026-05-18_22-21-00.md"

    markdown = report_path.read_text(encoding="utf-8")
    assert "# ZA Bank 中等置信度交易复核报告" in markdown
    assert "## Review Summary" in markdown
    assert "## Source-backed Items" in markdown
    assert "## Recommended Next Steps" in markdown
    assert "复核范围：`mapping_confidence = medium` 的 1 笔交易" in markdown
    assert "本批次只有 1 笔 medium 交易" in markdown
    assert "`local_transfer`：1 笔" in markdown
    assert "Local transfer 本地轉賬 HU QIN 012*******6557" in markdown
    assert "待判定_对外转账" in markdown
    assert "请确认 `HU QIN` 是你本人/关联账户，还是第三方收款人" in markdown
    assert "line `line:1:9`" in markdown


def test_render_za_medium_review_report_handles_multiple_medium_classes(tmp_path):
    review_module = load_module("render_za_medium_review_report_multi", REVIEW_SCRIPT_PATH)

    csv_path = tmp_path / "za_medium_rows.csv"
    fieldnames = [
        "transaction_id",
        "txn_date",
        "amount_signed",
        "description_raw",
        "txn_type",
        "category",
        "tag",
        "business_purpose",
        "accounting_subject",
        "mapping_note",
        "source_file",
        "source_line_ref",
        "counterparty_name_raw",
        "mapping_confidence",
    ]
    rows = [
        {
            "transaction_id": "txn_1",
            "txn_date": "2025-08-23",
            "amount_signed": "5",
            "description_raw": "Inward fund transfer | CHEN, GENG 393**********3987",
            "txn_type": "transfer_in",
            "category": "transfer_in",
            "tag": "self_transfer",
            "business_purpose": "本人名下资金转入",
            "accounting_subject": "内部资金往来",
            "mapping_note": "根据对手方姓名与账户持有人同名线索，推定为本人名下账户转入。",
            "source_file": "Statement_202508.pdf",
            "source_line_ref": "line:1:10",
            "counterparty_name_raw": "CHEN, GENG",
            "mapping_confidence": "medium",
        },
        {
            "transaction_id": "txn_2",
            "txn_date": "2025-11-05",
            "amount_signed": "-200",
            "description_raw": "OCL* OCTOPUS AD2792668",
            "txn_type": "wallet_topup_out",
            "category": "expense",
            "tag": "octopus",
            "business_purpose": "八达通充值",
            "accounting_subject": "交通费",
            "mapping_note": "Octopus 增值记录可判断为八达通充值，但具体对应后续哪类交通/零售消费仍待后续细分。",
            "source_file": "Statement_202511.pdf",
            "source_line_ref": "line:1:8",
            "counterparty_name_raw": "",
            "mapping_confidence": "medium",
        },
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report_path = review_module.save_za_medium_confidence_review_report(
        csv_path=csv_path,
        output_dir=tmp_path / "reports",
        timestamp_label="2026-05-20_07-10-00",
        generated_at_label="2026-05-20 07:10:00 CST",
    )

    markdown = report_path.read_text(encoding="utf-8")
    assert "`self_transfer`：1 笔" in markdown
    assert "`octopus`：1 笔" in markdown
    assert "请确认对应账户确实属于你本人或你控制的关联账户" in markdown
    assert "若你希望账务更粗颗粒，可继续保留在交通费" in markdown
    assert "HU QIN" not in markdown
