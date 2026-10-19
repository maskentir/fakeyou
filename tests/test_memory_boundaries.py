import ast
from pathlib import Path

import pytest

import fakeyou.debrief
from fakeyou.persistence.ingestion_status import RecordLockError, record_lock

DEBRIEF_DIR = Path(fakeyou.debrief.__file__).parent
GRAPH_EVIDENCE_MODULES = ("graphiti_core", "fakeyou.memory.memory", "fakeyou.memory.evidence")


def test_debrief_never_imports_the_graph_evidence_layer() -> None:
    offenders: list[str] = []
    for path in sorted(DEBRIEF_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name}: {name}" for name in names if name.startswith(GRAPH_EVIDENCE_MODULES)
            )
    assert offenders == []


def test_second_ingestion_trigger_is_rejected_by_the_record_lock(tmp_path: Path) -> None:
    record = tmp_path / "meeting-1" / "record.jsonl"
    record.parent.mkdir(parents=True)
    record.write_text("", encoding="utf-8")

    with (
        record_lock(record, purpose="ingestion"),
        pytest.raises(RecordLockError, match="locked"),
        record_lock(record, purpose="debrief"),
    ):
        pass
