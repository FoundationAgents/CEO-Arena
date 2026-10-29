"""Inference settings remain identical from TOML through frozen runtime identity."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ceo_arena.campaign import (
    CampaignApplication,
    CampaignCompiler,
    ExperimentSpec,
    FrozenCampaignPlan,
)
from ceo_arena.campaign.routes import ModelRouteRegistry
from ceo_arena.campaign.spec import ApiSpec, load_experiment_spec
from ceo_arena.config import default_scenario
from ceo_arena.harness.opencode import OpenCodeHarnessConfig
from ceo_arena.hashing import provider_endpoint_fingerprint
from ceo_arena.inference import InferenceSettings, resolve_inference
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.runtime.persistence import AgentAssignment, RunConfig

_MODEL = "gpt-5.6-sol"
_SECRET = "private-temporary-test-key"


def _spec(settings: InferenceSettings | None = None) -> ExperimentSpec:
    return ExperimentSpec(
        campaign_id="max-reasoning",
        base_url="https://newapi.example/v1",
        company_count=1,
        days=500,
        seeds=(11, 29, 47),
        api=(ApiSpec(api_key=SecretStr(_SECRET), models=(_MODEL,)),),
        inference={_MODEL: settings} if settings is not None else {},
    )


@pytest.mark.parametrize(
    ("model_id", "effort", "thinking", "output_field", "limit"),
    [
        ("gpt-6-astra", "max", None, "max_output_tokens", 128_000),
        ("gpt-5.6-sol", "max", None, "max_output_tokens", 128_000),
        ("claude-fable-5-1", "max", "adaptive", "max_tokens", 128_000),
        ("claude-sonnet-5", "max", "adaptive", "max_tokens", 128_000),
        ("deepseek-v4-pro", "max", "enabled", "max_tokens", 393_216),
        ("deepseek-v4-flash", "max", "enabled", "max_tokens", 393_216),
        ("qwen3.8-max", "xhigh", None, "max_completion_tokens", 65_536),
        ("gemini-3.8-flash", "high", None, "max_tokens", 65_536),
        ("doubao-seed-1.8", "high", "enabled", "max_completion_tokens", 65_536),
    ],
)
def test_known_profiles_use_highest_supported_settings(
    model_id: str, effort: str, thinking: str | None, output_field: str, limit: int
) -> None:
    settings = resolve_inference(model_id)

    assert settings.effort == effort
    assert settings.thinking == thinking
    assert settings.output_token_field == output_field
    assert settings.max_output_tokens == limit
    assert model_id in settings.expected_model_ids
    assert InferenceSettings.model_validate_json(settings.model_dump_json()) == settings


def test_reasoning_profiles_do_not_authorize_unverified_backend_aliases() -> None:
    assert resolve_inference("claude-fable-5-1").expected_model_ids == ("claude-fable-5-1",)
    assert resolve_inference("deepseek-v4-flash").expected_model_ids == ("deepseek-v4-flash",)
    assert resolve_inference("doubao-seed-1.8").expected_model_ids == (
        "doubao-seed-1.8",
        "doubao-seed-1-8-251228",
    )
    qwen = resolve_inference("qwen3.8-max")
    assert qwen.enable_thinking and qwen.preserve_thinking
    assert resolve_inference("offline-model") == InferenceSettings(
        protocol="openai-compatible-chat"
    )
    with pytest.raises(ValidationError, match="frozen"):
        qwen.effort = "low"


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": "openai-responses"},
        {"protocol": "openai-compatible-chat", "thinking": "adaptive"},
        {"protocol": "anthropic-messages", "enable_thinking": True},
        {"protocol": "anthropic-messages", "effort": "none"},
        {"protocol": "anthropic-messages", "thinking": "enabled"},
        {
            "protocol": "openai-compatible-chat",
            "thinking": "enabled",
            "enable_thinking": True,
        },
        {"protocol": "anthropic-messages", "output_token_field": "max_completion_tokens"},
        {"protocol": "openai-compatible-chat", "max_output_tokens": 0},
        {"protocol": "openai-compatible-chat", "max_output_tokens": 1 << 53},
        {"protocol": "openai-compatible-chat", "expected_model_ids": ["x", "x"]},
        {"protocol": "openai-compatible-chat", "expected_model_ids": ["bad model"]},
        {"protocol": "openai-compatible-chat", "api_key": "must-not-be-accepted"},
    ],
)
def test_invalid_inference_fields_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InferenceSettings.model_validate(payload)


def test_inference_changes_spec_plan_and_run_hash_without_leaking_keys() -> None:
    highest = _spec()
    lower = _spec(resolve_inference(_MODEL).model_copy(update={"effort": "high"}))
    plans = tuple(CampaignCompiler().compile(spec) for spec in (highest, lower))
    configs = tuple(
        RunConfig(
            run_id="same-run",
            scenario=default_scenario(),
            assignments=(
                AgentAssignment(
                    company_id="company-1",
                    model_id=_MODEL,
                    harness_id="opencode-native-test",
                    api_index=0,
                    inference=plan.routes[0].inference,
                ),
            ),
            provider_endpoint_sha256=provider_endpoint_fingerprint(highest.base_url),
        )
        for plan in plans
    )

    assert highest.spec_hash != lower.spec_hash
    assert plans[0].plan_hash != plans[1].plan_hash
    assert configs[0].config_hash != configs[1].config_hash
    for spec, plan, config in zip((highest, lower), plans, configs, strict=True):
        assert spec.inference[_MODEL] == plan.routes[0].inference == config.assignments[0].inference
        assert _SECRET not in plan.model_dump_json() + config.model_dump_json() + repr(spec)
        assert FrozenCampaignPlan.model_validate_json(plan.model_dump_json()) == plan
    with pytest.raises(ValueError, match="inference differs"):
        ModelRouteRegistry(lower).validate_plan(plans[0])


def test_settings_are_resolved_identically_without_an_explicit_override() -> None:
    automatic = _spec()
    explicit = _spec(resolve_inference(_MODEL))

    assert automatic.spec_hash == explicit.spec_hash
    assignment = AgentAssignment(
        company_id="company-1", model_id=_MODEL, harness_id="opencode-native-test", api_index=0
    )
    harness = OpenCodeHarnessConfig(
        base_url=automatic.base_url,
        model=_MODEL,
        api_key=SecretStr(_SECRET),
        worker_command=("unused-worker",),
    )
    assert assignment.inference == harness.inference == automatic.inference[_MODEL]
    rule = AgentAssignment(company_id="company-2", model_id=RULE_MODEL_ID, harness_id="rule")
    assert rule.inference is None
    with pytest.raises(ValidationError, match="non-provider"):
        AgentAssignment.model_validate({**rule.model_dump(), "inference": assignment.inference})


def test_reasoning_override_cannot_accidentally_disable_known_model_identity_checks() -> None:
    override = InferenceSettings(
        protocol="openai-responses", effort="high", output_token_field="max_output_tokens"
    )
    effective = _spec(override).inference[_MODEL]

    assert effective.effort == "high"
    assert effective.expected_model_ids == (_MODEL,)
    assert override.expected_model_ids == ()


def test_campaign_run_assignments_keep_the_explicit_frozen_profile(tmp_path: Path) -> None:
    spec = _spec(resolve_inference(_MODEL).model_copy(update={"effort": "high"}))
    plan = CampaignCompiler().compile(spec)
    application = CampaignApplication(spec, plan, tmp_path / "campaign")

    report = application.prepare()
    assert report.expected_runs == 6
    for run in plan.runs:
        config = application._run_config(run)
        assignment = config.assignments[0]
        assert assignment.inference == (
            spec.inference[_MODEL] if assignment.uses_provider else None
        )


def test_toml_profiles_roundtrip_and_unknown_routes_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "inference.toml"
    source.write_text(
        f'''campaign_id = "toml-profile"
base_url = "https://newapi.example/v1"
company_count = 1
days = 500
seeds = [11]
[[api]]
api_key = "{_SECRET}"
models = ["{_MODEL}"]
[inference."{_MODEL}"]
protocol = "openai-responses"
effort = "max"
output_token_field = "max_output_tokens"
max_output_tokens = 128000
expected_model_ids = ["{_MODEL}"]
''',
        encoding="utf-8",
    )
    spec = load_experiment_spec(source)
    assert spec.inference == {_MODEL: resolve_inference(_MODEL)}
    payload = spec.model_dump()
    payload["inference"] = {"unrouted-model": resolve_inference(_MODEL)}
    with pytest.raises(ValidationError, match="unrouted"):
        ExperimentSpec.model_validate(payload)
