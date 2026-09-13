"""Contract: the fixed-population recipe is valid on the current core.

The scenario's live certification must accept the effective policy built by the
recipe, with user-driven growth and CPU offload disabled, for both population
sizes. No site or worker process is started by this check.
"""

from pathlib import Path

import pytest
from genro_asgi.config.handler import ConfigurationHandler
from genro_asgi_multiworker_spa.orchestration.group_policy import GroupPolicy

from benchmarks.scenarios.eight_core_cycle.cycle_probe import REQUIRED_SETTINGS


@pytest.mark.parametrize("users_per_worker", [2, 15])
def test_fixed_recipe_matches_the_scenario_contract(monkeypatch, tmp_path, users_per_worker):
    monkeypatch.setenv("GNR_ASGI_ORCH_LOG", str(tmp_path / "orders.log"))
    monkeypatch.setenv("GNR_ASGI_WORKER_MAX_USERS", str(users_per_worker))
    recipe = Path(__file__).resolve().parents[1] / "benchmarks/scenarios/eight_core_cycle/cycle_recipe.py"
    pool = ConfigurationHandler(str(recipe)).group_kwargs("site")["pool"]
    policy = GroupPolicy.from_settings({
        key: value for key, value in pool.items() if key in GroupPolicy.SETPOINTS
    }).to_settings()
    assert policy["worker_max_number"] == 8
    assert policy["worker_max_users"] == users_per_worker
    for key, value in REQUIRED_SETTINGS["fixed"].items():
        assert key in policy and policy[key] == value
    assert pool["worker_class"] == "genropy_asgi.spa.genropy_worker:GenropyWorker"
    assert pool["engine_factory"] == "genropy_asgi.spa.site_engine_factory:GenropySiteEngineFactory"


def test_dynamic_recipe_has_no_user_cap_and_uses_current_admission_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("GNR_ASGI_ORCH_LOG", str(tmp_path / "orders.log"))
    monkeypatch.delenv("GNR_ASGI_WORKER_MAX_NUMBER", raising=False)
    recipe = Path(__file__).resolve().parents[1] / "benchmarks/scenarios/eight_core_cycle/dynamic_recipe.py"
    pool = ConfigurationHandler(str(recipe)).group_kwargs("site")["pool"]
    policy = GroupPolicy.from_settings({
        key: value for key, value in pool.items() if key in GroupPolicy.SETPOINTS
    }).to_settings()
    assert policy["worker_max_number"] == 16
    assert policy["worker_max_users"] is None
    for key, value in REQUIRED_SETTINGS["dynamic"].items():
        assert key in policy and policy[key] == value
