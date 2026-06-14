from __future__ import annotations

import csv
import importlib.util
from email.message import EmailMessage
from pathlib import Path

import fitz


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_za_email_regression.py"
INGESTION_SCRIPT_PATH = ROOT / "scripts" / "email_gateway_ingestion.py"
SCHEMA_PATH = ROOT / "schema-v2-draft.sql"


def load_module(name: str, path: Path):
    assert path.exists(), f"module missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def build_pdf_bytes_from_lines(lines: list[str]) -> bytes:
    pdf_doc = fitz.open()
    page = pdf_doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=12)
        y += 18
    payload = pdf_doc.tobytes()
    pdf_doc.close()
    return payload


def _build_za_regression_email() -> EmailMessage:
    za_lines = [
        "CONSOLIDATED MONTHLY STATEMENT 綜合月結單 01 Apr 2026 - 30 Apr 2026",
        "Account Number 賬戶號碼: 887027001-210 Statement Date 結單日期: 30 Apr 2026",
        "Deposit Summary 存款摘要 Closing Balance 本期結餘 32,005.65 HKD Equivalent 港元等值",
        "HKD Savings 港元活期儲蓄 (100.00%) 32,005.18",
        "FCY Savings 外幣活期儲蓄 (0.00%) 0.47",
        "Transaction History 交易歷史 HKD Savings 港元活期儲蓄",
        "31 Mar 2026 Opening balance 上期結餘 56,297.55",
        "01 Apr 2026 Interest 利息 7.63 56,305.18",
        "23 Apr 2026 Local transfer 本地轉賬 HU QIN 012*******6557 24,300.00 32,005.18",
        "Statement Date 結單日期: 30 Apr 2026",
    ]

    message = EmailMessage()
    message["From"] = "estatements@za.group"
    message["To"] = "gateway@example.com"
    message["Subject"] = "ZA Bank regression statement"
    message["Message-ID"] = "<za-regression@example.com>"
    message.set_content("demo body")
    message.add_attachment(
        build_pdf_bytes_from_lines(za_lines),
        maintype="application",
        subtype="pdf",
        filename="Statement_202604.pdf",
    )
    return message


def test_run_za_email_regression_rebuilds_and_exports_synthetic_email_db(tmp_path):
    regression_module = load_module("run_za_email_regression", SCRIPT_PATH)
    ingestion_module = load_module("email_gateway_ingestion_for_za_regression", INGESTION_SCRIPT_PATH)

    db_path = tmp_path / "email_ingestion.sqlite"
    attachments_dir = tmp_path / "attachments"
    raw_dir = tmp_path / "raw_emails"
    export_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"

    message = _build_za_regression_email()

    ingestion_module.ingest_email_message(
        message=message,
        db_path=db_path,
        schema_path=SCHEMA_PATH,
        attachments_dir=attachments_dir,
        raw_email_dir=raw_dir,
        source_channel="email_gateway",
        overwrite=False,
    )

    result = regression_module.run_za_email_regression(
        db_path=db_path,
        export_output_dir=export_dir,
        report_output_dir=report_dir,
        only_facts_built=True,
        save_export=True,
        save_report=True,
        timestamp_label="2026-05-20_01-15-00",
        generated_at_label="2026-05-20_01-15-00 HKT",
    )

    assert result["classify_summary"]["classified_documents"] == 1
    assert result["extract_summary"]["raw_document_lines"] == 10
    assert result["facts_summary"]["document_facts"] == 1
    assert result["builder_summary"] == {
        "documents_scanned": 1,
        "transactions_inserted": 2,
        "balance_markers_inserted": 3,
        "document_sections_inserted": 2,
        "duplicate_transactions_skipped": 0,
    }
    assert result["statement_rows"] == [
        {
            "statement_date": "2026-04-30",
            "document_id": "doc_att_email_za-regression@example.com_1",
            "filename": "Statement_202604.pdf",
            "txn_count": 2,
            "marker_count": 3,
            "section_count": 2,
        }
    ]

    export_result = result["export_result"]
    assert export_result is not None
    csv_path = Path(export_result["csv_path"])
    markdown_path = Path(export_result["markdown_path"])
    assert csv_path.exists()
    assert markdown_path.exists()
    assert csv_path.name == "za_transactions_unified_audit_2026-05-20_01-15-00.csv"
    assert markdown_path.name == "za_transactions_unified_audit_2026-05-20_01-15-00.md"

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert [row["description_raw"] for row in rows] == [
        "Interest",
        "Local transfer | HU QIN 012*******6557",
    ]

    medium_review_result = result["medium_review_result"]
    assert medium_review_result is not None
    medium_review_path = Path(medium_review_result["report_path"])
    assert medium_review_path.exists()
    assert medium_review_path.name == "za_medium_confidence_review_report_2026-05-20_01-15-00.md"
    assert medium_review_result["csv_path"] == str(csv_path)

    report_path = Path(result["report_path"])
    assert report_path.exists()
    assert report_path.name == "za_email_regression_2026-05-20_01-15-00.md"
    report_text = report_path.read_text(encoding="utf-8")
    assert "# ZA Email Regression Report" in report_text
    assert "| transactions_inserted | 2 |" in report_text
    assert "| 2026-04-30 | doc_att_email_za-regression@example.com_1 | Statement_202604.pdf | 2 | 3 | 2 |" in report_text
    assert "- 中置信度复核报告已保存: `True`" in report_text
    assert "- 复核报告 Markdown: `" in report_text
