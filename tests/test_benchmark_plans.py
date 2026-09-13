"""Contracts: repeatable plans are immutable inputs to both comparison legs."""

import hashlib
import json

import pytest

from benchmarks.execution.make_plans import make_plans


def scenario(tmp_path, expected):
    source = tmp_path / "scenario"
    source.mkdir()
    (source / "generate.py").write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[sys.argv.index('--out') + 1]).write_bytes(b'plan')\n")
    (source / "plans.spec.json").write_text(json.dumps({
        "generator": "generate.py", "plans": [
            {"name": "plan.json", "seed": 1, "args": [], "sha256": expected},
        ],
    }))
    return source


def test_valid_existing_plan_is_not_regenerated_and_corruption_is_rejected(tmp_path):
    digest = hashlib.sha256(b"plan").hexdigest()
    source = scenario(tmp_path, digest)
    output = tmp_path / "output"
    make_plans(source, output)
    # A second leg must not invoke a generator that could create different input.
    (source / "generate.py").write_text("raise RuntimeError('must not regenerate')\n")
    make_plans(source, output)
    certificate = (output / "plan.json.sha256").read_bytes()
    (output / "plan.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="expected"):
        make_plans(source, output)
    assert (output / "plan.json").read_bytes() == b"changed"
    assert (output / "plan.json.sha256").read_bytes() == certificate


def test_mismatching_generated_plan_is_not_published_or_certified(tmp_path):
    source = scenario(tmp_path, "0" * 64)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="expected"):
        make_plans(source, output)
    assert list(output.iterdir()) == []
