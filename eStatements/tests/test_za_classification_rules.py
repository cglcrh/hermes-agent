from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPORT_SCRIPT_PATH = ROOT / "scripts" / "export_za_unified_audit.py"


def load_module(name: str, path: Path):
    assert path.exists(), f"module missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_row(**overrides):
    row = {
        "description_raw": "",
        "channel": "",
        "direction": "credit",
        "counterparty_name_raw": "",
        "counterparty_raw": "",
        "counterparty_account_masked": "",
        "counterparty_phone_raw": "",
        "original_txn_type": "statement_cash_movement",
        "original_category": "unclassified",
        "original_tag": "unclassified",
        "original_business_purpose": "待补充业务用途",
        "original_accounting_subject": "待判定_未分类",
    }
    row.update(overrides)
    return row


def test_classify_za_coin_cash_rebate_as_reward_income():
    export_module = load_module("export_za_unified_audit_reward_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(description_raw="ZA Coin cash rebate", direction="credit")
    )

    assert result == {
        "txn_type": "reward_in",
        "category": "income",
        "tag": "promo_reward",
        "business_purpose": "营销返现入账",
        "accounting_subject": "营销奖励收入",
        "mapping_confidence": "high",
        "mapping_note": "cash rebate / rebate 关键词可直接判断为返现或奖励入账。",
    }


def test_classify_za_inward_transfer_from_self_as_internal_transfer_in():
    export_module = load_module("export_za_unified_audit_self_in_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="Inward fund transfer",
            channel="TRANSFER",
            direction="credit",
            counterparty_name_raw="CHEN, GENG",
            counterparty_raw="CHEN, GENG 393**********3987",
        )
    )

    assert result == {
        "txn_type": "transfer_in",
        "category": "transfer_in",
        "tag": "self_transfer",
        "business_purpose": "本人名下资金转入",
        "accounting_subject": "内部资金往来",
        "mapping_confidence": "medium",
        "mapping_note": "根据对手方姓名与账户持有人同名线索，推定为本人名下账户转入。",
    }


def test_classify_za_inward_transfer_with_preserved_counterparty_suffix_as_internal_transfer_in():
    export_module = load_module("export_za_unified_audit_self_in_suffix_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="Inward fund transfer | CHEN, GENG 393**********3987",
            channel="TRANSFER",
            direction="credit",
            counterparty_name_raw="CHEN, GENG",
            counterparty_raw="CHEN, GENG 393**********3987",
        )
    )

    assert result == {
        "txn_type": "transfer_in",
        "category": "transfer_in",
        "tag": "self_transfer",
        "business_purpose": "本人名下资金转入",
        "accounting_subject": "内部资金往来",
        "mapping_confidence": "medium",
        "mapping_note": "根据对手方姓名与账户持有人同名线索，推定为本人名下账户转入。",
    }


def test_classify_za_inward_transfer_from_third_party_as_fund_transfer_in():
    export_module = load_module("export_za_unified_audit_related_in_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="Inward fund transfer",
            channel="TRANSFER",
            direction="credit",
            counterparty_name_raw="MR KWONG TSZ HO",
            counterparty_raw="MR KWONG TSZ HO 691*****9833",
        )
    )

    assert result == {
        "txn_type": "transfer_in",
        "category": "transfer_in",
        "tag": "related_party_transfer_in",
        "business_purpose": "第三方或关联方资金转入",
        "accounting_subject": "待判定_资金往来流入",
        "mapping_confidence": "medium",
        "mapping_note": "可确认是转入款项，但仅凭结单仍无法区分借款、往来款或其他第三方来款。",
    }


def test_classify_za_fps_transfer_with_self_hint_as_internal_transfer_out():
    export_module = load_module("export_za_unified_audit_self_out_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="FPS transfer",
            channel="TRANSFER",
            direction="debit",
            counterparty_name_raw="CHEN G***",
            counterparty_raw="CHEN G*** +852-67370406",
            counterparty_phone_raw="+852-67370406",
        )
    )

    assert result == {
        "txn_type": "transfer_out",
        "category": "transfer_out",
        "tag": "self_transfer",
        "business_purpose": "本人名下资金转出",
        "accounting_subject": "内部转账支出",
        "mapping_confidence": "medium",
        "mapping_note": "根据对手方姓名与手机号线索，推定为本人名下账户资金调拨。",
    }


def test_classify_za_fps_transfer_with_preserved_counterparty_suffix_as_internal_transfer_out():
    export_module = load_module("export_za_unified_audit_self_out_suffix_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="FPS transfer | CHEN G*** +852-67370406",
            channel="TRANSFER",
            direction="debit",
            counterparty_name_raw="CHEN G***",
            counterparty_raw="CHEN G*** +852-67370406",
            counterparty_phone_raw="+852-67370406",
        )
    )

    assert result == {
        "txn_type": "transfer_out",
        "category": "transfer_out",
        "tag": "self_transfer",
        "business_purpose": "本人名下资金转出",
        "accounting_subject": "内部转账支出",
        "mapping_confidence": "medium",
        "mapping_note": "根据对手方姓名与手机号线索，推定为本人名下账户资金调拨。",
    }


def test_classify_za_local_transfer_as_external_transfer_out():
    export_module = load_module("export_za_unified_audit_local_transfer_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(
            description_raw="Local transfer",
            channel="TRANSFER",
            direction="debit",
            counterparty_name_raw="HU QIN",
            counterparty_raw="HU QIN 012*******6557",
        )
    )

    assert result == {
        "txn_type": "transfer_out",
        "category": "transfer_out",
        "tag": "local_transfer",
        "business_purpose": "本地转账支出",
        "accounting_subject": "待判定_对外转账",
        "mapping_confidence": "medium",
        "mapping_note": "可确认是本地转账支出，但仅凭样本无法判断是否属于本人账户划转或第三方往来。",
    }


def test_classify_za_transit_spend_as_transport_expense():
    export_module = load_module("export_za_unified_audit_transport_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(description_raw="MTR - Rides", direction="debit")
    )

    assert result == {
        "txn_type": "card_spend_out",
        "category": "expense",
        "tag": "transport",
        "business_purpose": "公共交通支出",
        "accounting_subject": "交通费",
        "mapping_confidence": "high",
        "mapping_note": "MTR / Tramway 等公共交通商户关键词可直接判断为交通支出。",
    }


def test_classify_za_octopus_topup_as_transport_wallet_topup():
    export_module = load_module("export_za_unified_audit_octopus_rules", EXPORT_SCRIPT_PATH)

    result = export_module._classify_transaction(
        make_row(description_raw="OCL* OCTOPUS AD2792668", direction="debit")
    )

    assert result == {
        "txn_type": "wallet_topup_out",
        "category": "expense",
        "tag": "octopus",
        "business_purpose": "八达通充值",
        "accounting_subject": "交通费",
        "mapping_confidence": "medium",
        "mapping_note": "Octopus 增值记录可判断为八达通充值，但具体对应后续哪类交通/零售消费仍待后续细分。",
    }
