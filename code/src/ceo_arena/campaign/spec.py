"""TOML input model for local multi-key campaign execution."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from ceo_arena.campaign.models import RULE_MODEL_ID, CampaignExecutionSpec
from ceo_arena.harness.opencode import validate_newapi_base_url
from ceo_arena.inference import InferenceSettings, resolve_inference
from ceo_arena.models import StrictModel


def _validate_model_id(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("model ids must be non-empty and cannot have surrounding whitespace")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("model ids cannot contain whitespace or control characters")
    if value == RULE_MODEL_ID:
        raise ValueError("the deterministic rule CEO is added automatically")
    return value


class ApiSpec(StrictModel):
    """One ephemeral API key and the models it is allowed to route."""

    api_key: SecretStr = Field(min_length=1)
    models: tuple[str, ...]

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, api_key: SecretStr) -> SecretStr:
        if not api_key.get_secret_value().strip():
            raise ValueError("API keys cannot be empty or whitespace")
        return api_key

    @field_validator("models")
    @classmethod
    def validate_models(cls, models: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(_validate_model_id(model_id) for model_id in models)
        if not validated:
            raise ValueError("each API entry requires at least one model")
        if len(validated) != len(set(validated)):
            raise ValueError("models within one API entry must be unique")
        return validated


class ExperimentSpec(StrictModel):
    """Complete local experiment input, including ephemeral credentials."""

    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    base_url: str = Field(min_length=1)
    scenario: Path | None = None
    company_count: int = Field(ge=1)
    days: int = Field(ge=1)
    seeds: tuple[int, ...]
    api: tuple[ApiSpec, ...]
    model_order: tuple[str, ...] = ()
    inference: dict[str, InferenceSettings] = Field(default_factory=dict)
    execution: CampaignExecutionSpec = Field(default_factory=CampaignExecutionSpec)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return validate_newapi_base_url(value)

    @model_validator(mode="after")
    def validate_experiment(self) -> ExperimentSpec:
        if not self.seeds or len(self.seeds) != len(set(self.seeds)):
            raise ValueError("experiment seeds must be non-empty and unique")
        if not self.api:
            raise ValueError("experiment requires at least one API entry")
        api_keys = [entry.api_key.get_secret_value() for entry in self.api]
        if len(api_keys) != len(set(api_keys)):
            raise ValueError("API entries must use distinct keys")
        routed_models = tuple(model_id for entry in self.api for model_id in entry.models)
        if len(routed_models) != len(set(routed_models)):
            raise ValueError("model ids must be globally unique across API entries")
        order = tuple(_validate_model_id(model_id) for model_id in self.model_order)
        if not order:
            order = routed_models
            self.model_order = order
        if len(order) != len(set(order)) or set(order) != set(routed_models):
            raise ValueError("model_order must contain every routed model exactly once")
        if unknown := set(self.inference) - set(routed_models):
            raise ValueError(
                f"inference settings reference unrouted models: {', '.join(sorted(unknown))}"
            )
        self.inference = {
            model_id: resolve_inference(model_id, self.inference.get(model_id))
            for model_id in order
        }
        if self.company_count != len(routed_models):
            raise ValueError(
                "company_count must equal the number of provider models for the "
                "complete Leave-One-Out design"
            )
        return self

    @property
    def model_ids(self) -> tuple[str, ...]:
        """Return provider-backed model identities in user-declared order."""

        return self.model_order

    @property
    def spec_hash(self) -> str:
        """Hash reproducibility fields while allowing API key replacement on resume."""

        payload = self.model_dump(
            mode="json",
            exclude={"scenario": True, "api": {"__all__": {"api_key"}}},
        )
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def load_experiment_spec(path: Path) -> ExperimentSpec:
    """Load one strict TOML spec and resolve its optional scenario path."""

    source = path.resolve()
    payload = tomllib.loads(source.read_text(encoding="utf-8"))
    try:
        spec = ExperimentSpec.model_validate(payload)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors(include_input=False)
        )
        raise ValueError(f"invalid Campaign spec: {details}") from None
    if spec.scenario is None or spec.scenario.is_absolute():
        return spec
    return spec.model_copy(update={"scenario": (source.parent / spec.scenario).resolve()})


__all__ = ["ApiSpec", "ExperimentSpec", "load_experiment_spec"]
