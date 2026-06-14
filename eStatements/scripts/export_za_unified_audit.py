from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unified_audit_export_shared import (
    render_unified_audit_markdown as render_shared_unified_audit_markdown,
    save_unified_audit_export as save_shared_unified_audit_export,
)

DEFAULT_DB_PATH = PROJECT_ROOT / "2026-05-05_za_bank_sample_01" / "db" / "transactions_v2.sqlite"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "za_transactions_export"

CSV_FIELDNAMES = [
    "row_no",
    "transaction_id",
    "institution_id",
    "institution_name",
    "account_id",
    "account_masked",
    "account_name",
    "account_type",
    "owner_entity",
    "document_id",
    "statement_date",
    "statement_period_start",
    "statement_period_end",
    "source_file",
    "source_page",
    "source_line_ref",
    "source_extraction_method",
    "txn_date",
    "posting_date",
    "effective_date",
    "currency",
    "amount",
    "direction",
    "amount_signed",
    "description_raw",
    "description_clean",
    "reference_no",
    "bank_reference",
    "external_reference",
    "counterparty_raw",
    "counterparty_name_raw",
    "counterparty_normalized",
    "counterparty_account_masked",
    "counterparty_phone_raw",
    "counterparty_bank_name",
    "channel",
    "payment_rail",
    "balance",
    "balance_currency",
    "continuity_check_status",
    "txn_type",
    "category",
    "tag",
    "business_purpose",
    "accounting_subject",
    "mapping_confidence",
    "mapping_note",
    "confidence",
    "needs_review",
    "review_reason",
    "created_at",
]


def _render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    header_line = "| " + " | ".join(headers) + " |"
    divider_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    body_lines = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_line, divider_line, *body_lines])


def _default_timestamp_label() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def _default_generated_at_label() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S %Z")


def _contains_self_name(text: str) -> bool:
    upper = (text or "").upper()
    return any(token in upper for token in ("CHEN GENG", "CHEN, GENG", "CHEN G***"))


def _classify_transaction(row: sqlite3.Row) -> dict[str, str]:
    description_raw = row["description_raw"] or ""
    description = description_raw.upper()
    channel = (row["channel"] or "").lower()
    direction = (row["direction"] or "").lower()
    counterparty_name_raw = row["counterparty_name_raw"] or ""
    counterparty_raw = row["counterparty_raw"] or ""
    counterparty_phone_raw = row["counterparty_phone_raw"] or ""

    if "INTEREST" in description or channel in {"interest_credit", "interest"}:
        return {
            "txn_type": "interest_in",
            "category": "income",
            "tag": "interest",
            "business_purpose": "存款利息入账",
            "accounting_subject": "利息收入",
            "mapping_confidence": "high",
            "mapping_note": "利息关键词与渠道信息直接命中。",
        }

    if direction == "credit" and "REBATE" in description:
        return {
            "txn_type": "reward_in",
            "category": "income",
            "tag": "promo_reward",
            "business_purpose": "营销返现入账",
            "accounting_subject": "营销奖励收入",
            "mapping_confidence": "high",
            "mapping_note": "cash rebate / rebate 关键词可直接判断为返现或奖励入账。",
        }

    if direction == "debit" and ("TRAMWAY" in description or "MTR" in description):
        return {
            "txn_type": "card_spend_out",
            "category": "expense",
            "tag": "transport",
            "business_purpose": "公共交通支出",
            "accounting_subject": "交通费",
            "mapping_confidence": "high",
            "mapping_note": "MTR / Tramway 等公共交通商户关键词可直接判断为交通支出。",
        }

    if direction == "debit" and "OCTOPUS" in description:
        return {
            "txn_type": "wallet_topup_out",
            "category": "expense",
            "tag": "octopus",
            "business_purpose": "八达通充值",
            "accounting_subject": "交通费",
            "mapping_confidence": "medium",
            "mapping_note": "Octopus 增值记录可判断为八达通充值，但具体对应后续哪类交通/零售消费仍待后续细分。",
        }

    if description.startswith("INWARD FUND TRANSFER") and channel == "transfer" and direction == "credit":
        if _contains_self_name(counterparty_name_raw) or _contains_self_name(counterparty_raw):
            return {
                "txn_type": "transfer_in",
                "category": "transfer_in",
                "tag": "self_transfer",
                "business_purpose": "本人名下资金转入",
                "accounting_subject": "内部资金往来",
                "mapping_confidence": "medium",
                "mapping_note": "根据对手方姓名与账户持有人同名线索，推定为本人名下账户转入。",
            }
        return {
            "txn_type": "transfer_in",
            "category": "transfer_in",
            "tag": "related_party_transfer_in",
            "business_purpose": "第三方或关联方资金转入",
            "accounting_subject": "待判定_资金往来流入",
            "mapping_confidence": "medium",
            "mapping_note": "可确认是转入款项，但仅凭结单仍无法区分借款、往来款或其他第三方来款。",
        }

    if description.startswith("FPS TRANSFER") and channel == "transfer" and direction == "debit" and (
        _contains_self_name(counterparty_name_raw)
        or _contains_self_name(counterparty_raw)
        or "67370406" in counterparty_phone_raw
        or "67370406" in counterparty_raw
    ):
        return {
            "txn_type": "transfer_out",
            "category": "transfer_out",
            "tag": "self_transfer",
            "business_purpose": "本人名下资金转出",
            "accounting_subject": "内部转账支出",
            "mapping_confidence": "medium",
            "mapping_note": "根据对手方姓名与手机号线索，推定为本人名下账户资金调拨。",
        }

    if direction == "debit" and (
        channel == "local_transfer"
        or (channel == "transfer" and description.startswith("LOCAL TRANSFER"))
    ):
        return {
            "txn_type": "transfer_out",
            "category": "transfer_out",
            "tag": "local_transfer",
            "business_purpose": "本地转账支出",
            "accounting_subject": "待判定_对外转账",
            "mapping_confidence": "medium",
            "mapping_note": "可确认是本地转账支出，但仅凭样本无法判断是否属于本人账户划转或第三方往来。",
        }

    return {
        "txn_type": row["original_txn_type"] or "unclassified",
        "category": row["original_category"] or "unclassified",
        "tag": row["original_tag"] or "unclassified",
        "business_purpose": row["original_business_purpose"] or "待补充业务用途",
        "accounting_subject": row["original_accounting_subject"] or "待判定_未分类",
        "mapping_confidence": "low",
        "mapping_note": "暂无 ZA Bank 专用规则，需人工补充分类。",
    }


def fetch_za_unified_audit_rows(db_path: str | Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT
            t.transaction_id,
            i.institution_code AS institution_id,
            i.institution_name,
            a.account_id,
            a.account_masked,
            a.account_name,
            a.account_type,
            a.owner_entity,
            t.document_id,
            d.statement_date,
            d.period_start AS statement_period_start,
            d.period_end AS statement_period_end,
            t.source_file,
            t.source_page,
            t.source_line_ref,
            t.source_extraction_method,
            t.txn_date,
            t.posting_date,
            t.effective_date,
            t.currency,
            t.amount,
            t.direction,
            t.amount_signed,
            t.description_raw,
            t.description_clean,
            t.reference_no,
            t.bank_reference,
            t.external_reference,
            t.counterparty_raw,
            t.counterparty_name_raw,
            COALESCE(t.counterparty_clean, t.counterparty_name_clean) AS counterparty_normalized,
            t.counterparty_account_masked,
            t.counterparty_phone_raw,
            t.counterparty_bank_name,
            t.channel,
            t.payment_rail,
            t.balance,
            t.balance_currency,
            t.continuity_check_status,
            t.txn_type AS original_txn_type,
            t.category AS original_category,
            t.tag AS original_tag,
            t.business_purpose AS original_business_purpose,
            t.accounting_subject AS original_accounting_subject,
            t.confidence,
            t.needs_review,
            t.review_reason,
            t.created_at
        FROM transactions t
        JOIN documents d ON d.document_id = t.document_id
        LEFT JOIN accounts a ON a.account_id = t.account_id
        LEFT JOIN institutions i ON i.institution_id = t.institution_id
        WHERE i.institution_code = 'za_bank'
        ORDER BY t.txn_date, t.source_line_ref, t.transaction_id
    """
    records = conn.execute(query).fetchall()
    conn.close()

    rows: list[dict[str, object]] = []
    for index, record in enumerate(records, start=1):
        mapping = _classify_transaction(record)
        rows.append(
            {
                "row_no": index,
                "transaction_id": record["transaction_id"],
                "institution_id": record["institution_id"],
                "institution_name": record["institution_name"],
                "account_id": record["account_id"],
                "account_masked": record["account_masked"],
                "account_name": record["account_name"],
                "account_type": record["account_type"],
                "owner_entity": record["owner_entity"],
                "document_id": record["document_id"],
                "statement_date": record["statement_date"],
                "statement_period_start": record["statement_period_start"],
                "statement_period_end": record["statement_period_end"],
                "source_file": record["source_file"],
                "source_page": record["source_page"],
                "source_line_ref": record["source_line_ref"],
                "source_extraction_method": record["source_extraction_method"],
                "txn_date": record["txn_date"],
                "posting_date": record["posting_date"],
                "effective_date": record["effective_date"],
                "currency": record["currency"],
                "amount": record["amount"],
                "direction": record["direction"],
                "amount_signed": record["amount_signed"],
                "description_raw": record["description_raw"],
                "description_clean": record["description_clean"],
                "reference_no": record["reference_no"],
                "bank_reference": record["bank_reference"],
                "external_reference": record["external_reference"],
                "counterparty_raw": record["counterparty_raw"],
                "counterparty_name_raw": record["counterparty_name_raw"],
                "counterparty_normalized": record["counterparty_normalized"],
                "counterparty_account_masked": record["counterparty_account_masked"],
                "counterparty_phone_raw": record["counterparty_phone_raw"],
                "counterparty_bank_name": record["counterparty_bank_name"],
                "channel": record["channel"],
                "payment_rail": record["payment_rail"],
                "balance": record["balance"],
                "balance_currency": record["balance_currency"],
                "continuity_check_status": record["continuity_check_status"],
                "txn_type": mapping["txn_type"],
                "category": mapping["category"],
                "tag": mapping["tag"],
                "business_purpose": mapping["business_purpose"],
                "accounting_subject": mapping["accounting_subject"],
                "mapping_confidence": mapping["mapping_confidence"],
                "mapping_note": mapping["mapping_note"],
                "confidence": record["confidence"],
                "needs_review": record["needs_review"],
                "review_reason": record["review_reason"],
                "created_at": record["created_at"],
            }
        )
    return rows


def render_za_unified_audit_markdown(
    *,
    csv_path: str | Path,
    rows: list[dict[str, object]],
    db_path: str | Path,
    generated_at_label: str,
) -> str:
    return render_shared_unified_audit_markdown(
        title="ZA Bank 统一审计/记账格式导出",
        csv_path=csv_path,
        rows=rows,
        db_path=db_path,
        generated_at_label=generated_at_label,
        key_field_lines=[
            "- `txn_type`: 机器可复用交易类型，如 `interest_in` / `transfer_out`",
            "- `category`: 更稳定的大类，如 `income` / `transfer_out`",
            "- `tag`: 细粒度标签，如 `interest` / `local_transfer`",
            "- `business_purpose`: 面向审计说明的业务用途描述",
            "- `accounting_subject`: 面向记账的建议科目，保守场景会明确写成 `待判定_*`",
            "- `mapping_confidence`: 本次规则映射置信度，便于后续优先复核",
            "- `mapping_note`: 解释为什么这样分类型/挂科目",
        ],
        rule_summary_lines=[
            "- `Interest 利息` / `interest_credit` → `interest_in` / `income` / `利息收入`",
            "- `Local transfer 本地轉賬 ...`（debit）→ `transfer_out` / `transfer_out` / `待判定_对外转账`",
        ],
        usage_lines=[
            "- 可直接用 CSV 做后续记账映射、透视表、审计抽样。",
            "- 建议优先复核 `mapping_confidence = medium/low` 的行，尤其是 `待判定_对外转账`。",
            "- 本导出保留了 `document_id`、`source_file`、`source_page`、`source_line_ref`，可回溯到原结单证据。",
        ],
        file_note_lines=[
            "> 说明：本次 `accounting_subject` 是“保守建议科目”，重点是让后续审计/记账更容易，而不是假装所有业务性质都已最终确认。",
        ],
        net_amount_header="net_amount_hkd",
    )


def save_za_unified_audit_export(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    timestamp_label: str | None = None,
    generated_at_label: str | None = None,
) -> dict[str, object]:
    timestamp_label = timestamp_label or _default_timestamp_label()
    generated_at_label = generated_at_label or _default_generated_at_label()
    rows = fetch_za_unified_audit_rows(db_path)

    csv_path = Path(output_dir) / f"za_transactions_unified_audit_{timestamp_label}.csv"
    markdown = render_za_unified_audit_markdown(
        csv_path=csv_path,
        rows=rows,
        db_path=db_path,
        generated_at_label=generated_at_label,
    )
    return save_shared_unified_audit_export(
        rows=rows,
        fieldnames=CSV_FIELDNAMES,
        output_dir=output_dir,
        file_prefix="za_transactions_unified_audit",
        timestamp_label=timestamp_label,
        markdown=markdown,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export ZA Bank transactions into unified audit CSV and markdown summary.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to the v2 SQLite database.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory to save CSV/markdown outputs.")
    parser.add_argument("--timestamp-label", default=None, help="Optional timestamp label for deterministic filenames.")
    parser.add_argument("--generated-at-label", default=None, help="Optional display label written into markdown.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = save_za_unified_audit_export(
        db_path=args.db_path,
        output_dir=args.output_dir,
        timestamp_label=args.timestamp_label,
        generated_at_label=args.generated_at_label,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
