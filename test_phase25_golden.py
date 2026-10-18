"""Phase 2.5 golden output: replay the synthetic fixtures with frozen inputs and compare the result
with the committed tests/fixtures/synthetic_golden.json. Any difference means a code change moved
what TaskDash derives; read it, then regenerate if the change is intended:

    python _tools/golden_replay.py synthetic --out tests/fixtures/synthetic_golden.json

(run with the ArcGIS Pro interpreter like the tests; add --now T to change the frozen clock, which
must match the one the fixtures were generated with, see test_phase2_synthetic.py)
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = FIXTURES / "synthetic_golden.json"


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "_tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


golden = _load_tool("golden_replay")


def test_replay_matches_the_committed_golden(tmp_path, capsys):
    out = tmp_path / "now.json"
    assert golden.main(["synthetic", "--out", str(out), "--fixtures-dir", str(FIXTURES)]) == 0
    assert golden.main(["compare", str(GOLDEN), str(out)]) == 0, capsys.readouterr().out


def test_committed_golden_has_metrics_changes_and_per_host_alerts():
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(data) == {"inventory", "runs", "metrics", "changes", "alert"}
    assert len(data["inventory"]) == 25 and data["runs"]
    assert data["metrics"], "metric logs were not mined: the golden would not guard the miner"
    assert [c["change_kind"] for c in data["changes"]] == ["definition_changed"]
    assert set(data["alert"]) == {"APP-HOST-01", "APP-HOST-02"}


def test_golden_holds_no_temp_path_or_user_name():
    text = GOLDEN.read_text(encoding="utf-8")
    assert "<LOGS>" in text and "Users" not in text
