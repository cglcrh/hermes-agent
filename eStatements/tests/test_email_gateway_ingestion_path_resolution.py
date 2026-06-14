from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INGESTION_SCRIPT_PATH = ROOT / "scripts" / "email_gateway_ingestion.py"


def load_module(name: str, path: Path):
    assert path.exists(), f"module missing: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_stored_path_falls_back_to_project_root(monkeypatch, tmp_path):
    ingestion_module = load_module("email_gateway_ingestion_path_resolution", INGESTION_SCRIPT_PATH)

    target_path = ROOT / tmp_path.name / "attachments" / "sample.pdf"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("demo", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    resolved = ingestion_module.resolve_stored_path(f"{tmp_path.name}/attachments/sample.pdf")

    assert resolved == target_path.resolve()
