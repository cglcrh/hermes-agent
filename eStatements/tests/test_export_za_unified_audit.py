from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SCRIPT_PATH = ROOT / "scripts" / "migrate_za_bank_to_v2.py"
EXPORT_SCRIPT_PATH = ROOT / "scripts" / "export_za_unified_audit.py"
PROJECT_DIR = ROOT / "2026-05-05_za_bank_sample_01"
SCHEMA_PATH = ROOT / "schema-v2-draft.sql"


def load_module(name: str, path: Path):
    assert path.exists(), f"module missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_export_za_unified_audit_creates_expected_csv_and_markdown(tmp_path):
    migrate_module = load_module("migrate_za_bank_to_v2_for_unified_export", MIGRATE_SCRIPT_PATH)
    export_module = load_module("export_za_unified_audit", EXPORT_SCRIPT_PATH)

    db_path = tmp_path / "za_transactions_v2.sqlite"
    migrate_module.migrate_project(
        project_dir=PROJECT_DIR,
        schema_path=SCHEMA_PATH,
        output_path=db_path,
        overwrite=True,
    )

    result = export_module.save_za_unified_audit_export(
        db_path=db_path,
        output_dir=tmp_path / "reports",
        timestamp_label="2026-05-18_22-10-00",
        generated_at_label="2026-05-18_22-10-00 CST",
    )

    csv_path = Path(result["csv_path"])
    md_path = Path(result["markdown_path"])

    assert csv_path.name == "za_transactions_unified_audit_2026-05-18_22-10-00.csv"
    assert md_path.name == "za_transactions_unified_audit_2026-05-18_22-10-00.md"

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 2
    assert rows[0]["institution_id"] == "za_bank"
    assert rows[0]["account_masked"] == "887027001-210"
    assert rows[0]["txn_type"] == "interest_in"
    assert rows[0]["category"] == "income"
    assert rows[0]["tag"] == "interest"
    assert rows[0]["business_purpose"] == "存款利息入账"
    assert rows[0]["accounting_subject"] == "利息收入"
    assert rows[0]["mapping_confidence"] == "high"
    assert rows[0]["source_line_ref"] == "line:1:8"

    assert rows[1]["txn_type"] == "transfer_out"
    assert rows[1]["category"] == "transfer_out"
    assert rows[1]["tag"] == "local_transfer"
    assert rows[1]["business_purpose"] == "本地转账支出"
    assert rows[1]["accounting_subject"] == "待判定_对外转账"
    assert rows[1]["mapping_confidence"] == "medium"
    assert rows[1]["mapping_note"] == "可确认是本地转账支出，但仅凭样本无法判断是否属于本人账户划转或第三方往来。"
    assert rows[1]["source_line_ref"] == "line:1:9"

    markdown = md_path.read_text(encoding="utf-8")
    assert "# ZA Bank 统一审计/记账格式导出" in markdown
    assert "交易总笔数: `2`" in markdown
    assert "高置信度分类: `1`" in markdown
    assert "中置信度分类: `1`" in markdown
    assert "利息收入" in markdown
    assert "待判定_对外转账" in markdown
    assert "Local transfer 本地轉賬 HU QIN 012*******6557" in markdown


def test_export_za_unified_audit_supports_email_ingestion_db_schema(tmp_path):
    export_module = load_module("export_za_unified_audit_email_db", EXPORT_SCRIPT_PATH)

    source_db = ROOT / "db" / "email_ingestion.sqlite"
    db_path = tmp_path / "email_ingestion.sqlite"
    db_path.write_bytes(source_db.read_bytes())

    result = export_module.save_za_unified_audit_export(
        db_path=db_path,
        output_dir=tmp_path / "reports",
        timestamp_label="2026-05-19_22-30-00",
        generated_at_label="2026-05-19_22-30-00 CST",
    )

    csv_path = Path(result["csv_path"])
    md_path = Path(result["markdown_path"])

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 19
    assert result["low_confidence_count"] == 0
    assert rows[0]["institution_id"] == "za_bank"
    assert md_path.exists()
