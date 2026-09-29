"""基线校验脚本单测：阈值与报告路径的参数化。

为什么要给一个「CI 脚本」写单测：`scripts/check_baseline.py` 现在**同时把守两个 CI job**——
`test` job 的 dev 口径（阈值 0.72）与 `bge` job 的夜间口径（阈值 0.95、另一份报告）。
这类「一处参数解析、两处生效」的共享逻辑，一旦写错（环境变量名拼错、路径拼接漏了根目录），
表现是**两个 job 一起静默失效**——比报错更糟：CI 照样绿，只是再也不会拦任何东西。

所以这里锁四件事：默认阈值、阈值覆盖、报告路径的三种来源、以及「该红时必须红」。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "check_baseline.py"


def _load(monkeypatch, **env) -> object:
    """在受控环境变量下加载脚本模块（阈值是模块级常量，必须加载前设好）。"""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("_check_baseline_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _write_report(path: Path, pass_rate: float, passed: int = 1, total: int = 1) -> Path:
    path.write_text(json.dumps({
        "pass_rate": pass_rate,
        "passed": passed,
        "total": total,
        "avg_recall": 1.0,
        "avg_confidence": 0.5,
        "latency_p50_ms": 1.0,
        "latency_p95_ms": 2.0,
        "failed_cases": [],
    }, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 阈值
# --------------------------------------------------------------------------- #
class TestThreshold:
    def test_default_is_dev_baseline(self, monkeypatch):
        mod = _load(monkeypatch, KB_BASELINE_MIN=None)
        assert mod.MIN_PASS_RATE == 0.72, "dev 口径默认阈值被改动（会让 test job 松紧变化）"

    def test_env_override(self, monkeypatch):
        mod = _load(monkeypatch, KB_BASELINE_MIN="0.95")
        assert mod.MIN_PASS_RATE == 0.95, "bge 口径阈值覆盖失效"

    def test_bad_value_fails_loudly(self, monkeypatch):
        """阈值写错必须炸，而不是静默退回默认值——静默退回等于把守的门自己开了。"""
        with pytest.raises(ValueError):
            _load(monkeypatch, KB_BASELINE_MIN="不是数字")


# --------------------------------------------------------------------------- #
# 报告路径
# --------------------------------------------------------------------------- #
class TestReportPath:
    def test_default_is_root_eval_report(self, monkeypatch):
        mod = _load(monkeypatch, KB_EVAL_REPORT=None)
        monkeypatch.setattr(sys, "argv", ["check_baseline.py"])
        assert Path(mod._report_path()) == _ROOT / "eval_report.json"

    def test_env_overrides(self, monkeypatch):
        mod = _load(monkeypatch, KB_EVAL_REPORT="eval_report_bge.json")
        monkeypatch.setattr(sys, "argv", ["check_baseline.py"])
        assert Path(mod._report_path()) == _ROOT / "eval_report_bge.json"

    def test_argv_beats_env(self, monkeypatch):
        mod = _load(monkeypatch, KB_EVAL_REPORT="from_env.json")
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", "from_argv.json"])
        assert Path(mod._report_path()) == _ROOT / "from_argv.json", "命令行参数应优先于环境变量"

    def test_absolute_path_kept(self, monkeypatch, tmp_path):
        mod = _load(monkeypatch, KB_EVAL_REPORT=None)
        p = tmp_path / "abs.json"
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", str(p)])
        assert Path(mod._report_path()) == p

    def test_relative_is_resolved_against_repo_root(self, monkeypatch):
        """相对路径按**仓库根**解析，而不是当前工作目录——
        否则在子目录里跑就会「找不到报告」，而 CI 与本地之所以能一致，
        靠的正是这个口径。"""
        mod = _load(monkeypatch, KB_EVAL_REPORT=None)
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", "sub/x.json"])
        assert Path(mod._report_path()) == _ROOT / "sub" / "x.json"


# --------------------------------------------------------------------------- #
# 判定（该绿要绿、该红必须红）
# --------------------------------------------------------------------------- #
class TestGate:
    def test_passes_at_threshold_boundary(self, monkeypatch, tmp_path, capsys):
        mod = _load(monkeypatch, KB_BASELINE_MIN="0.72")
        rep = _write_report(tmp_path / "r.json", 0.72, passed=72, total=100)
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", str(rep)])
        assert mod.main() == 0, "恰好等于阈值应放行（>= 语义）"

    def test_fails_below_threshold(self, monkeypatch, tmp_path, capsys):
        mod = _load(monkeypatch, KB_BASELINE_MIN="0.95")
        rep = _write_report(tmp_path / "r.json", 0.91, passed=10, total=11)
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", str(rep)])
        assert mod.main() == 1, "低于阈值必须返回非零（否则 CI 不会红）"
        assert "低于基线" in capsys.readouterr().err

    def test_missing_report_fails(self, monkeypatch, tmp_path, capsys):
        mod = _load(monkeypatch, KB_BASELINE_MIN=None)
        monkeypatch.setattr(sys, "argv", ["check_baseline.py", str(tmp_path / "nope.json")])
        assert mod.main() == 1, "报告缺失必须失败——不能因为「没报告」就当通过"
        assert "未找到" in capsys.readouterr().err
