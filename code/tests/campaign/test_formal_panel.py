"""Keep the formal model pool, routes and publication labels consistent."""

import re
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

from ceo_arena.campaign import CampaignCompiler, load_experiment_spec

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "campaign.example.toml"
MODEL_ORDER = (
    "gpt-6-astra",
    "deepseek-v4-pro",
    "claude-sonnet-5",
    "qwen3.8-max",
    "gpt-5.6-sol",
    "gemini-3.8-flash",
    "deepseek-v4-flash",
    "doubao-seed-1.8",
)
API_MODELS = (
    ("gpt-6-astra", "gpt-5.6-sol"),
    ("claude-sonnet-5",),
    ("deepseek-v4-pro", "qwen3.8-max", "gemini-3.8-flash", "deepseek-v4-flash", "doubao-seed-1.8"),
)
RULE = "deterministic-rule-ceo"


@pytest.fixture
def paper_contract_audit() -> ModuleType:
    """Load the manuscript audit only when its separately held sources exist."""
    return pytest.importorskip(
        "paper.scripts.check_code_alignment",
        reason="The released supplement does not include the manuscript audit module.",
    )


def test_formal_example_removes_fable_without_changing_other_routes_or_settings() -> None:
    spec = load_experiment_spec(EXAMPLE)
    assert spec.model_ids == MODEL_ORDER
    assert spec.company_count == 8
    assert tuple(api.models for api in spec.api) == API_MODELS
    assert set(spec.inference) == set(MODEL_ORDER)
    assert [spec.inference[model].effort for model in MODEL_ORDER] == [
        "max",
        "max",
        "max",
        "xhigh",
        "max",
        "high",
        "max",
        "high",
    ]
    assert [spec.inference[model].max_output_tokens for model in MODEL_ORDER] == [
        128000,
        393216,
        128000,
        65536,
        128000,
        65536,
        393216,
        65536,
    ]
    assert spec.execution.max_concurrent_runs == 30
    assert spec.execution.schedule == "seed_priority"


def test_formal_plan_has_twenty_seven_fixed_seat_runs_and_balanced_appearances() -> None:
    spec = load_experiment_spec(EXAMPLE)
    plan = CampaignCompiler().compile(spec)
    assert len(plan.runs) == 27
    assert len(plan.lineups) == 9
    assert plan.days == 500
    assert plan.seeds == (11, 29, 47)
    route_indices = {model: index for index, models in enumerate(API_MODELS) for model in models}
    appearances: Counter[str] = Counter()
    seed_appearances: Counter[tuple[int, str]] = Counter()
    for run in plan.runs:
        assert len(run.model_ids) == len(run.company_ids) == 8
        assert run.model_ids.count(RULE) <= 1
        for seat, model in enumerate(run.model_ids):
            assert model == RULE or model == MODEL_ORDER[seat]
            assert run.api_indices[seat] == route_indices.get(model)
            appearances[model] += 1
            seed_appearances[run.seed, model] += 1
    assert appearances == {model: 24 for model in (*MODEL_ORDER, RULE)}
    assert seed_appearances == {
        (seed, model): 8 for seed in plan.seeds for model in (*MODEL_ORDER, RULE)
    }
    assert Counter(run.seed for run in plan.runs) == {11: 9, 29: 9, 47: 9}
    assert plan.estimate.total_company_trajectories == 216
    assert plan.estimate.model_company_trajectories == 192
    assert plan.estimate.rule_company_trajectories == 24


def test_formal_paper_model_tables_and_labels_match_the_example(
    paper_contract_audit: ModuleType,
) -> None:
    audit = paper_contract_audit.PaperContractAudit(ROOT, EXAMPLE)
    models = audit.check_panel()
    audit.check_labels(models)


def test_formal_deepseek_variants_have_distinct_publication_labels(
    paper_contract_audit: ModuleType,
) -> None:
    labels = paper_contract_audit.PaperContractAudit(ROOT, EXAMPLE).label_map("SHORT_LABELS")
    assert labels["deepseek-v4-flash"] != labels["deepseek-v4-pro"]


def test_formal_panel_document_matches_example_routes() -> None:
    spec = load_experiment_spec(EXAMPLE)
    document = (ROOT / "docs/FORMAL_MODEL_PANEL.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\d+) \| `([^`]+)` \| (\d+) \|$", document, re.MULTILINE)
    routes = {model: str(index) for index, api in enumerate(spec.api, 1) for model in api.models}
    assert rows == [
        (str(seat), model, routes[model]) for seat, model in enumerate(spec.model_ids, 1)
    ]
