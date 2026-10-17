from pathlib import Path

import pytest

from company_brain.build import build
from company_brain.llm import LLM

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "acme"


@pytest.fixture(scope="session")
def rules_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("g") / "rules.db"
    build(DATA, db, None, use_model=False)
    return db


@pytest.fixture(scope="session")
def full_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The full graph, with model extraction replayed from the recorded cache (no network)."""
    db = tmp_path_factory.mktemp("g") / "full.db"
    build(DATA, db, LLM(offline=True), use_model=True)
    return db


@pytest.fixture()
def offline_llm() -> LLM:
    return LLM(offline=True)
