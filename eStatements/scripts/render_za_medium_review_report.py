from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from medium_review_shared import (
    load_medium_confidence_rows,
    render_medium_review_report,
    save_medium_review_report,
)

DEFAULT_EXPORT_DIR = PROJECT_ROOT / "reports" / "za_transactions_export"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports" / "za_transactions_export"


def _format_amount(amount_signed: str) -> str:
    value = float(amount_signed)
    sign = "+" if value >= 0 else "-"
    return f"{sign}HKD {abs(value):,.2f}"


def _render_tag_summary(rows: list[dict[str, str]]) -> list[str]:
    tag_counter = Counter((row.get("tag") or "unclassified") for row in rows)
    lines = [f"- 本批次只有 {len(rows)} 笔 medium 交易"]
    for tag, count in sorted(tag_counter.items()):
        lines.append(f"- `{tag}`：{count} 笔")
    return lines


def _build_review_guidance(row: dict[str, str]) -> tuple[str, str]:
    tag = (row.get("tag") or "").strip().lower()
    counterparty_name = (row.get("counterparty_name_raw") or "").strip()

    if tag == "local_transfer":
        name_hint = counterparty_name or "收款方"
        return (
            "中：可以确认是本地转账支出，但收款方归属和真实业务性质仍未锁定。",
            f"请确认 `{name_hint}` 是你本人/关联账户，还是第三方收款人；如果是本人账户可改成内部资金划转，如果是第三方则继续细分为往来款、费用或其他业务付款。",
        )

    if tag == "self_transfer":
        return (
            "中：已较强地指向本人名下账户间调拨，但仍建议保留一层人工确认，避免把第三方代收代付误判成内部转账。",
            "请确认对应账户确实属于你本人或你控制的关联账户；若只是代收代付或临时过桥，请改挂更贴近实际业务的往来科目。",
        )

    if tag == "related_party_transfer_in":
        return (
            "中：可以确认是外部或关联方转入，但资金性质仍未锁定，可能是借款、还款、垫付款回流或其他往来。",
            "请补一句该笔入账背景：是借款、还款、代收回款还是其他关联往来；确认后可进一步落到更明确的往来或负债科目。",
        )

    if tag == "octopus":
        return (
            "中：当前可先视为八达通充值/交通钱包补值，但仅凭银行流水仍无法判断后续实际消费是否全部应归入交通费。",
            "若你希望账务更粗颗粒，可继续保留在交通费；若后续要做更细分类，可把这类交易单独挂到电子钱包充值，再按消费明细拆分。",
        )

    return (
        "中：当前规则已给出一个可工作的临时分类，但仍建议结合业务背景做一次人工确认。",
        "请补充这笔交易的实际用途、对手方关系和是否属于本人账户调拨，再决定是否需要细化分类。",
    )


def _build_review_summary_lines(rows: list[dict[str, str]]) -> list[str]:
    return [
        *_render_tag_summary(rows),
        "",
        "### 重点结论",
        "",
        "1. medium 不代表规则不可用，而是代表**当前分类仍依赖少量业务背景确认**。",
        "2. 对 ZA 来说，medium 主要集中在**账户调拨、对外转账、关联方来款、钱包充值**这几类。",
        "3. 你通常只要补一句“这笔钱是转给谁/从谁来/实际用途是什么”，就能把它们快速定稿。",
    ]


def _build_source_backed_item_lines(rows: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        risk, action = _build_review_guidance(row)
        lines.extend(
            [
                f"### {index}. {row['txn_date']} · {_format_amount(row['amount_signed'])}",
                "",
                f"- `transaction_id`: `{row['transaction_id']}`",
                f"- 原始描述：`{row['description_raw']}`",
                f"- 当前映射：`{row['txn_type']}` / `{row['category']}` / `{row['tag']}`",
                f"- 当前业务用途：{row['business_purpose'] or '—'}",
                f"- 当前会计科目：{row['accounting_subject'] or '—'}",
                f"- 现有备注：{row['mapping_note'] or '—'}",
                f"- 证据定位：`{row['source_file']}` · line `{row['source_line_ref']}`",
                f"- 复核风险：{risk}",
                f"- 建议动作：{action}",
                "",
            ]
        )
    return lines


def _build_recommended_next_step_lines() -> list[str]:
    return [
        "### 建议你优先补充的信息",
        "",
        "- 这笔交易的对手方与你是什么关系：本人、关联方、客户、供应商，还是独立第三方。",
        "- 这笔交易的真实用途：账户调拨、借还款、往来款、消费、充值或其他。",
        "- 若你手头有聊天记录、银行通知或备注，补一句即可显著提高定稿速度。",
        "",
        "### 可直接保留的临时结论",
        "",
        "- 在未补证据前，这些交易继续保留 **medium** 是合理的。",
        "- 现有分类已经可以支持初步审计/记账，不会因为暂未定稿而完全不可用。",
        "- 等你确认关键背景后，这批交易可以很快下沉到最终科目。",
    ]


def render_za_medium_confidence_review_report(
    *,
    csv_path: str | Path,
    generated_at_label: str,
) -> str:
    rows = load_medium_confidence_rows(csv_path)
    return render_medium_review_report(
        title="ZA Bank 中等置信度交易复核报告",
        csv_path=csv_path,
        generated_at_label=generated_at_label,
        review_scope_label=f"`mapping_confidence = medium` 的 {len(rows)} 笔交易",
        purpose_line="- 目的：先保留粗分类，再把最需要你补充业务背景的交易单独挑出来。",
        review_summary_lines=_build_review_summary_lines(rows),
        source_backed_item_lines=_build_source_backed_item_lines(rows),
        recommended_next_step_lines=_build_recommended_next_step_lines(),
    )


def save_za_medium_confidence_review_report(
    *,
    csv_path: str | Path,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    timestamp_label: str,
    generated_at_label: str,
) -> Path:
    markdown = render_za_medium_confidence_review_report(
        csv_path=csv_path,
        generated_at_label=generated_at_label,
    )
    return save_medium_review_report(
        markdown=markdown,
        output_dir=output_dir,
        file_prefix="za_medium_confidence_review_report",
        timestamp_label=timestamp_label,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render ZA Bank medium-confidence review report from unified audit CSV.")
    parser.add_argument("--csv-path", required=True, help="Path to the ZA unified audit CSV.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory to save the markdown report.")
    parser.add_argument("--timestamp-label", required=True, help="Filename timestamp label.")
    parser.add_argument("--generated-at-label", required=True, help="Display timestamp label shown inside the report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_path = save_za_medium_confidence_review_report(
        csv_path=args.csv_path,
        output_dir=args.output_dir,
        timestamp_label=args.timestamp_label,
        generated_at_label=args.generated_at_label,
    )
    print(report_path)


if __name__ == "__main__":
    main()
