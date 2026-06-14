from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from email_gateway_ingestion import (  # type: ignore
    build_document_facts_from_ingested_email_documents,
    build_za_transactions_from_ingested_email_documents,
    classify_ingested_email_documents,
    extract_raw_text_for_ingested_email_documents,
)
from email_regression_shared import (  # type: ignore
    build_export_and_medium_review_artifacts,
    default_generated_at_label,
    default_timestamp_label,
    render_email_regression_report,
    save_email_regression_report,
)
from export_za_unified_audit import save_za_unified_audit_export  # type: ignore
from render_za_medium_review_report import save_za_medium_confidence_review_report  # type: ignore

DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "email_ingestion.sqlite"
DEFAULT_CLASSIFIED_OUTPUT_DIR = PROJECT_ROOT / "reports" / "za_classified_from_email"
DEFAULT_EXPORT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "za_transactions_export_from_email_db"
DEFAULT_REPORT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "za_email_regression"


def _summary_rows(summary: dict[str, object], keys: list[str]) -> list[list[str]]:
    return [[key, str(summary.get(key, 0))] for key in keys]


def fetch_za_statement_rows(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    only_facts_built: bool = True,
) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT
                d.document_id,
                COALESCE(MAX(df.statement_date), '') AS statement_date,
                d.filename,
                COUNT(DISTINCT t.transaction_id) AS txn_count,
                COUNT(DISTINCT bm.balance_marker_id) AS marker_count,
                COUNT(DISTINCT ds.section_id) AS section_count
            FROM documents d
            LEFT JOIN document_facts df ON df.document_id = d.document_id
            LEFT JOIN transactions t ON t.document_id = d.document_id
            LEFT JOIN balance_markers bm ON bm.document_id = d.document_id
            LEFT JOIN document_sections ds ON ds.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'za_bank'
        """
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " GROUP BY d.document_id, d.filename ORDER BY statement_date, d.document_id"
        rows = conn.execute(query).fetchall()
        return [
            {
                "statement_date": row["statement_date"],
                "document_id": row["document_id"],
                "filename": row["filename"],
                "txn_count": row["txn_count"],
                "marker_count": row["marker_count"],
                "section_count": row["section_count"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def render_za_email_regression_report(
    *,
    db_path: str | Path,
    classify_summary: dict[str, object],
    extract_summary: dict[str, int],
    facts_summary: dict[str, int],
    builder_summary: dict[str, int],
    statement_rows: list[dict[str, object]],
    export_result: dict[str, object] | None,
    medium_review_result: dict[str, object] | None,
    generated_at_label: str,
    only_facts_built: bool,
) -> str:
    return render_email_regression_report(
        title="ZA Email Regression Report",
        db_path=db_path,
        generated_at_label=generated_at_label,
        only_facts_built=only_facts_built,
        summary_sections=[
            {
                "title": "Classification Summary",
                "headers": ["Metric", "Value"],
                "rows": _summary_rows(
                    classify_summary,
                    [
                        "documents_scanned",
                        "classified_documents",
                        "unclassified_documents",
                        "ignored_documents",
                        "missing_source_documents",
                        "profile_stubs_written",
                    ],
                ),
            },
            {
                "title": "Raw Text Extraction Summary",
                "headers": ["Metric", "Value"],
                "rows": _summary_rows(
                    extract_summary,
                    [
                        "raw_document_pages",
                        "raw_document_lines",
                        "documents_updated",
                    ],
                ),
            },
            {
                "title": "Document Facts Summary",
                "headers": ["Metric", "Value"],
                "rows": _summary_rows(
                    facts_summary,
                    [
                        "document_facts",
                        "documents_updated",
                    ],
                ),
            },
            {
                "title": "Builder Summary",
                "headers": ["Metric", "Value"],
                "rows": _summary_rows(
                    builder_summary,
                    [
                        "documents_scanned",
                        "transactions_inserted",
                        "balance_markers_inserted",
                        "document_sections_inserted",
                        "duplicate_transactions_skipped",
                    ],
                ),
            },
        ],
        statement_rows=statement_rows,
        export_result=export_result,
        medium_review_result=medium_review_result,
    )


def save_za_email_regression_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    report_output_dir: str | Path = DEFAULT_REPORT_OUTPUT_DIR,
    classify_summary: dict[str, object],
    extract_summary: dict[str, int],
    facts_summary: dict[str, int],
    builder_summary: dict[str, int],
    statement_rows: list[dict[str, object]],
    export_result: dict[str, object] | None,
    medium_review_result: dict[str, object] | None,
    timestamp_label: str | None = None,
    generated_at_label: str | None = None,
    only_facts_built: bool = True,
) -> Path:
    timestamp_label = timestamp_label or default_timestamp_label()
    generated_at_label = generated_at_label or default_generated_at_label()
    markdown = render_za_email_regression_report(
        db_path=db_path,
        classify_summary=classify_summary,
        extract_summary=extract_summary,
        facts_summary=facts_summary,
        builder_summary=builder_summary,
        statement_rows=statement_rows,
        export_result=export_result,
        medium_review_result=medium_review_result,
        generated_at_label=generated_at_label,
        only_facts_built=only_facts_built,
    )
    return save_email_regression_report(
        markdown=markdown,
        output_dir=report_output_dir,
        file_prefix="za_email_regression",
        timestamp_label=timestamp_label,
    )


def run_za_email_regression(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    classified_output_dir: str | Path = DEFAULT_CLASSIFIED_OUTPUT_DIR,
    export_output_dir: str | Path = DEFAULT_EXPORT_OUTPUT_DIR,
    report_output_dir: str | Path = DEFAULT_REPORT_OUTPUT_DIR,
    only_facts_built: bool = True,
    save_export: bool = True,
    save_report: bool = True,
    timestamp_label: str | None = None,
    generated_at_label: str | None = None,
) -> dict[str, object]:
    timestamp_label = timestamp_label or default_timestamp_label()
    generated_at_label = generated_at_label or default_generated_at_label()

    classify_summary = classify_ingested_email_documents(
        db_path=db_path,
        output_dir=classified_output_dir,
        link_mode="copy",
    )
    extract_summary = extract_raw_text_for_ingested_email_documents(
        db_path=db_path,
        only_classified=True,
    )
    facts_summary = build_document_facts_from_ingested_email_documents(
        db_path=db_path,
        only_raw_extracted=True,
    )
    builder_summary = build_za_transactions_from_ingested_email_documents(
        db_path=db_path,
        only_facts_built=only_facts_built,
    )
    statement_rows = fetch_za_statement_rows(
        db_path=db_path,
        only_facts_built=only_facts_built,
    )

    export_result, medium_review_result = build_export_and_medium_review_artifacts(
        save_export=save_export,
        export_save_fn=save_za_unified_audit_export,
        export_kwargs={
            "db_path": db_path,
            "output_dir": export_output_dir,
            "timestamp_label": timestamp_label,
            "generated_at_label": generated_at_label,
        },
        medium_review_save_fn=save_za_medium_confidence_review_report,
        medium_review_kwargs={
            "output_dir": export_output_dir,
            "timestamp_label": timestamp_label,
            "generated_at_label": generated_at_label,
        },
    )

    report_path = None
    if save_report:
        report_path = save_za_email_regression_report(
            db_path=db_path,
            report_output_dir=report_output_dir,
            classify_summary=classify_summary,
            extract_summary=extract_summary,
            facts_summary=facts_summary,
            builder_summary=builder_summary,
            statement_rows=statement_rows,
            export_result=export_result,
            medium_review_result=medium_review_result,
            timestamp_label=timestamp_label,
            generated_at_label=generated_at_label,
            only_facts_built=only_facts_built,
        )

    return {
        "generated_at": generated_at_label,
        "db_path": str(Path(db_path)),
        "only_facts_built": only_facts_built,
        "classify_summary": classify_summary,
        "extract_summary": extract_summary,
        "facts_summary": facts_summary,
        "builder_summary": builder_summary,
        "statement_rows": statement_rows,
        "export_result": export_result,
        "medium_review_result": medium_review_result,
        "report_path": str(report_path) if report_path else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild ZA Bank transactions from the email-ingestion DB and save regression artifacts.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to the email-ingestion SQLite DB.")
    parser.add_argument("--classified-output-dir", default=str(DEFAULT_CLASSIFIED_OUTPUT_DIR), help="Directory for email classification artifacts.")
    parser.add_argument("--export-output-dir", default=str(DEFAULT_EXPORT_OUTPUT_DIR), help="Directory for unified audit CSV/markdown export.")
    parser.add_argument("--report-output-dir", default=str(DEFAULT_REPORT_OUTPUT_DIR), help="Directory for the ZA regression markdown report.")
    parser.add_argument("--timestamp-label", default=None, help="Optional deterministic timestamp label for output filenames.")
    parser.add_argument("--generated-at-label", default=None, help="Optional display label used inside markdown/json output.")
    parser.add_argument("--include-non-facts-built", action="store_true", help="Include ZA documents not yet at document_facts_built status.")
    parser.add_argument("--skip-export", action="store_true", help="Do not save the unified audit CSV/markdown export.")
    parser.add_argument("--skip-report", action="store_true", help="Do not save the regression markdown report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_za_email_regression(
        db_path=args.db_path,
        classified_output_dir=args.classified_output_dir,
        export_output_dir=args.export_output_dir,
        report_output_dir=args.report_output_dir,
        only_facts_built=not args.include_non_facts_built,
        save_export=not args.skip_export,
        save_report=not args.skip_report,
        timestamp_label=args.timestamp_label,
        generated_at_label=args.generated_at_label,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
