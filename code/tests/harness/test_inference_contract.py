"""Cross-language contract test for frozen settings and native worker profiles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ceo_arena.harness.bun import resolve_bun_executable
from ceo_arena.inference import InferenceSettings, resolve_inference

ROOT = Path(__file__).resolve().parents[2]
MODELS = (
    "gpt-6-astra",
    "gpt-5.6-sol",
    "claude-fable-5-1",
    "claude-sonnet-5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "qwen3.8-max",
    "gemini-3.8-flash",
    "doubao-seed-1.8",
    "offline-model",
)


def test_python_and_native_worker_resolve_identical_effective_profiles() -> None:
    override = InferenceSettings(
        protocol="openai-responses", effort="high", output_token_field="max_output_tokens"
    )
    cases = (
        *((model, None) for model in MODELS),
        ("gpt-5.6-sol", override),
        ("doubao-seed-1.8", resolve_inference("doubao-seed-1.8")),
        ("offline-model", override),
        (
            "gpt-5.6-sol",
            override.model_copy(update={"expected_model_ids": ("explicit-model-version",)}),
        ),
    )
    payload = [
        {"model": model, "settings": settings.model_dump(mode="json") if settings else None}
        for model, settings in cases
    ]
    process = subprocess.run(
        [resolve_bun_executable(ROOT), "run", "tests/inference_profiles.ts"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT / "agent_worker",
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr[:2_000]
    actual = json.loads(process.stdout)
    for (model, override), result in zip(cases, actual, strict=True):
        expected = resolve_inference(model, override)
        assert result["profile"] == expected.model_dump(mode="json")
        cap = expected.max_output_tokens
        assert result["limits"] == [
            cap,
            min(2_048, cap) if cap is not None else 2_048,
            min(131_072, cap) if cap is not None else 131_072,
        ]
