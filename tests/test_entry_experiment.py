from jupiter_trading.entry_comparison import build_comparison, export_csv
from jupiter_trading.entry_experiment import VARIANTS, ExperimentConfig, ExperimentIdentity
from jupiter_trading.research_store import ResearchStore


def _run(label, mode, status="COMPLETED", pnl=0.0):
    return {
        "id": f"run-{label}",
        "experiment_id": "exp-test",
        "variant_label": label,
        "shared_config_hash": "same-hash",
        "status": status,
        "initial_equity": 1_000_000,
        "session_pnl": pnl,
        "config": {"signal_strategy": mode},
        "portfolio": {"initial_cash": 1_000_000, "equity": 1_000_000 + pnl},
        "metrics": {"fees": 0},
        "fills": [],
        "events": [],
        "open_positions": [],
    }


def test_experiment_configuration_is_frozen_and_hash_is_stable() -> None:
    first = ExperimentConfig()
    second = ExperimentConfig()

    assert first.shared_hash == second.shared_hash
    assert first.resolved()["experiment_kind"] == "THREE_MACD_ENTRIES_SHARED_EXIT_V1"
    assert first.resolved()["momentum_exit_version"] == "BEARISH_MACD_V1"


def test_experiment_identity_creates_three_distinct_accounts() -> None:
    identity = ExperimentIdentity.create("experiment-1")
    accounts = [identity.account_id(label) for label, _mode in VARIANTS]
    assert accounts == ["experiment-1-A", "experiment-1-B", "experiment-1-C"]


def test_comparison_requires_all_three_completed_matching_runs() -> None:
    experiment = {
        "experiment_id": "exp-test",
        "session_id": "2026-09-10",
        "shared_config_hash": "same-hash",
        "resolved_config": ExperimentConfig().resolved(),
    }
    runs = [_run(label, mode) for label, mode in VARIANTS]
    complete = build_comparison(experiment, runs)
    incomplete = build_comparison(experiment, runs[:2])

    assert complete["comparison_status"] == "COMPLETE"
    assert incomplete["comparison_status"] == "INCOMPLETE"
    assert export_csv("equity", runs).splitlines()[0]


def test_experiment_metadata_survives_store_restart(tmp_path) -> None:
    path = str(tmp_path / "research.db")
    payload = {
        "experiment_id": "exp-test",
        "session_id": "2026-09-10",
        "status": "RUNNING",
        "shared_config_hash": "hash",
        "variants": [],
    }
    ResearchStore(path).save_experiment(payload)

    restored = ResearchStore(path)
    assert restored.experiment("exp-test")["shared_config_hash"] == "hash"
    assert restored.experiments()[0]["experiment_id"] == "exp-test"

