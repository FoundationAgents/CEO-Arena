"""Secret-free, frozen provider settings shared by plans and model workers."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ceo_arena.models import StrictModel


class InferenceSettings(StrictModel):
    """Describe the exact provider protocol, reasoning mode, and output ceiling."""

    model_config = ConfigDict(frozen=True)

    protocol: Literal["openai-responses", "anthropic-messages", "openai-compatible-chat"]
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    thinking: Literal["adaptive", "enabled"] | None = None
    enable_thinking: bool = False
    preserve_thinking: bool = False
    output_token_field: Literal["max_output_tokens", "max_tokens", "max_completion_tokens"] = (
        "max_tokens"
    )
    max_output_tokens: int | None = Field(default=None, gt=0, le=(1 << 53) - 1)
    expected_model_ids: tuple[str, ...] = ()

    @field_validator("expected_model_ids")
    @classmethod
    def validate_model_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value or any(character.isspace() or ord(character) < 32 for character in value)
            for value in values
        ):
            raise ValueError("expected model identities must be non-empty, unique model ids")
        return values

    @model_validator(mode="after")
    def validate_protocol_fields(self) -> InferenceSettings:
        if self.protocol == "openai-responses":
            if self.output_token_field != "max_output_tokens" or self.thinking is not None:
                raise ValueError("Responses requires max_output_tokens and reasoning effort")
        elif self.protocol == "anthropic-messages":
            if self.output_token_field != "max_tokens" or self.thinking == "enabled":
                raise ValueError("Messages requires max_tokens and adaptive thinking")
            if self.effort == "none":
                raise ValueError("Messages does not support effort none")
        elif self.output_token_field == "max_output_tokens" or self.thinking == "adaptive":
            raise ValueError("Chat requires a compatible output field and enabled thinking")
        if self.protocol != "openai-compatible-chat" and (
            self.enable_thinking or self.preserve_thinking
        ):
            raise ValueError("enable_thinking and preserve_thinking require Chat")
        if self.thinking is not None and self.enable_thinking:
            raise ValueError("thinking and enable_thinking are alternative provider switches")
        return self


_RESPONSES = InferenceSettings(
    protocol="openai-responses",
    effort="max",
    output_token_field="max_output_tokens",
    max_output_tokens=128_000,
)
_MESSAGES = InferenceSettings(
    protocol="anthropic-messages",
    effort="max",
    thinking="adaptive",
    max_output_tokens=128_000,
)
_DEEPSEEK = InferenceSettings(
    protocol="openai-compatible-chat",
    effort="max",
    thinking="enabled",
    max_output_tokens=393_216,
)
_PROFILES = {
    "gpt-6-astra": _RESPONSES,
    "gpt-5.6-sol": _RESPONSES,
    "claude-fable-5-1": _MESSAGES,
    "claude-sonnet-5": _MESSAGES,
    "deepseek-v4-pro": _DEEPSEEK,
    "deepseek-v4-flash": _DEEPSEEK,
    "qwen3.8-max": InferenceSettings(
        protocol="openai-compatible-chat",
        effort="xhigh",
        enable_thinking=True,
        preserve_thinking=True,
        output_token_field="max_completion_tokens",
        max_output_tokens=65_536,
    ),
    "gemini-3.8-flash": InferenceSettings(
        protocol="openai-compatible-chat",
        effort="high",
        max_output_tokens=65_536,
    ),
    "doubao-seed-1.8": InferenceSettings(
        protocol="openai-compatible-chat",
        effort="high",
        thinking="enabled",
        output_token_field="max_completion_tokens",
        max_output_tokens=65_536,
        expected_model_ids=("doubao-seed-1.8", "doubao-seed-1-8-251228"),
    ),
}


def resolve_inference(
    model_id: str, override: InferenceSettings | None = None
) -> InferenceSettings:
    """Resolve an explicit profile or the researched highest-supported model defaults."""

    researched = _PROFILES.get(model_id)
    profile = override or researched or InferenceSettings(protocol="openai-compatible-chat")
    if researched is None or profile.expected_model_ids:
        return profile
    return profile.model_copy(
        update={"expected_model_ids": researched.expected_model_ids or (model_id,)}
    )


__all__ = ["InferenceSettings", "resolve_inference"]
