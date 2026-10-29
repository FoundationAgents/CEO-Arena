from __future__ import annotations

import csv
import math
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ceo_arena.evaluation import (
    ArenaAnalysis,
    ModelHealth,
    TrackAScore,
    TrackBBackground,
    TrackBDistribution,
    TrackCComparison,
    TrackCSeedComparison,
)
from ceo_arena.reporting import build_paper_report, write_paper_artifacts

RULE_MODEL_ID = "deterministic-rule-ceo"
MODEL_IDS = tuple(f"model-{index}" for index in range(1, 10))
ALL_IDS = (*MODEL_IDS, RULE_MODEL_ID)


def _analysis(model_count: int = 9) -> ArenaAnalysis:
    model_ids = tuple(f"model-{index}" for index in range(1, model_count + 1))
    all_ids = (*model_ids, RULE_MODEL_ID)
    track_a: dict[str, TrackAScore] = {}
    track_b: dict[str, TrackBDistribution] = {}
    health: dict[str, ModelHealth] = {}
    for model_index, model_id in enumerate(all_ids):
        backgrounds = tuple(
            TrackBBackground(
                model_ids=tuple(item for item in all_ids if item not in {model_id, omitted}),
                seed_scores_cents=(
                    (model_index + 1) * 100_000_000 + omitted_index * 10_000_000 - 1_000_000,
                    (model_index + 1) * 100_000_000 + omitted_index * 10_000_000,
                    (model_index + 1) * 100_000_000 + omitted_index * 10_000_000 + 1_000_000,
                ),
                mean_cents=(model_index + 1) * 100_000_000 + omitted_index * 10_000_000,
            )
            for omitted_index, omitted in enumerate(item for item in all_ids if item != model_id)
        )
        scores = tuple(score for item in backgrounds for score in item.seed_scores_cents)
        means = tuple(item.mean_cents for item in backgrounds)
        mean = statistics.fmean(scores)
        track_a[model_id] = TrackAScore(
            model_id=model_id,
            run_count=model_count * 3,
            mean_official_score_cents=mean,
        )
        track_b[model_id] = TrackBDistribution(
            model_id=model_id,
            run_count=model_count * 3,
            background_count=model_count,
            scores_cents=scores,
            mean_cents=mean,
            between_background_std_cents=statistics.pstdev(means),
            within_background_std_cents=math.sqrt(2 / 3) * 1_000_000,
            backgrounds=backgrounds,
            minimum_cents=min(scores),
            maximum_cents=max(scores),
        )
        health[model_id] = ModelHealth(
            model_id=model_id,
            decision_count=model_count * 3,
            failure_count=0,
            failure_rate=0,
            status_counts={"sealed": model_count * 3},
        )

    track_c: dict[str, TrackCComparison] = {}
    for entrant_index, entrant in enumerate(model_ids, start=1):
        affected_ids = tuple(sorted(item for item in model_ids if item != entrant))
        comparisons: list[TrackCSeedComparison] = []
        for seed in (11, 29, 47):
            seed_shift = {11: -1, 29: 0, 47: 1}[seed]
            effects = {
                affected: (index + entrant_index + seed_shift) * 1_000_000
                for index, affected in enumerate(affected_ids, start=1)
            }
            private = entrant_index * 10_000_000 + seed_shift * 1_000_000
            others = sum(effects.values())
            comparisons.append(
                TrackCSeedComparison(
                    seed=seed,
                    background_model_ids=affected_ids,
                    target_company_id=f"company-{entrant_index}",
                    private_difference_cents=private,
                    others_difference_cents=others,
                    system_difference_cents=private + others,
                    directed_effects_cents=effects,
                )
            )
        track_c[entrant] = TrackCComparison(
            model_id=entrant,
            baseline_model_id=RULE_MODEL_ID,
            pair_count=3,
            seed_comparisons=tuple(comparisons),
            mean_private_difference_cents=statistics.fmean(
                item.private_difference_cents for item in comparisons
            ),
            mean_others_difference_cents=statistics.fmean(
                item.others_difference_cents for item in comparisons
            ),
            mean_system_difference_cents=statistics.fmean(
                item.system_difference_cents for item in comparisons
            ),
            mean_directed_effects_cents={
                affected: statistics.fmean(
                    item.directed_effects_cents[affected] for item in comparisons
                )
                for affected in affected_ids
            },
            positive_system_seed_count=3,
        )
    return ArenaAnalysis(track_a=track_a, track_b=track_b, track_c=track_c, health=health)


def test_build_report_uses_nine_means_and_complete_directed_matrix() -> None:
    report = build_paper_report(
        _analysis(),
        labels={RULE_MODEL_ID: "Rule CEO"},
        bankruptcy_rates={model_id: 0.0 for model_id in ALL_IDS},
        economic_threshold_cents=1_000_000,
    )

    assert len(report.performance) == 10
    assert all(len(item.background_means_cents) == 9 for item in report.performance)
    assert len(report.replacements) == 9
    assert all(len(item.seed_points) == 3 for item in report.replacements)
    assert len(report.externalities) == 72
    assert all(item.affected_model_id != item.entrant_model_id for item in report.externalities)
    assert all(item.highlighted for item in report.externalities)
    assert "correlation" not in report.model_dump_json().lower()
    assert "additiv" not in report.model_dump_json().lower()


def test_build_report_rejects_non_final_background_count() -> None:
    analysis = _analysis()
    track = analysis.track_b[MODEL_IDS[0]]
    analysis.track_b[MODEL_IDS[0]] = track.model_copy(
        update={
            "background_count": 8,
            "backgrounds": track.backgrounds[:8],
            "run_count": 24,
        }
    )

    with pytest.raises(ValueError, match="9 backgrounds and 3 seeds"):
        build_paper_report(analysis)


def test_write_artifacts_produces_parseable_svg_and_expected_csv(tmp_path: Path) -> None:
    report = build_paper_report(_analysis())
    assert report.economic_threshold_cents is None
    assert not any(item.highlighted for item in report.externalities)

    manifest = write_paper_artifacts(report, tmp_path)

    svg_paths = (
        manifest.figure_2a_svg,
        manifest.figure_2b_svg,
        manifest.figure_3a_svg,
        manifest.figure_3b_svg,
    )
    assert all(path.is_file() for path in svg_paths)
    for path in svg_paths:
        root = ET.parse(path).getroot()
        assert root.tag.endswith("svg")
        assert root.attrib["viewBox"]
    figure_2a = ET.parse(manifest.figure_2a_svg).getroot()
    assert len(figure_2a.findall("{http://www.w3.org/2000/svg}circle")) == 90

    with manifest.table_2_csv.open(encoding="utf-8", newline="") as stream:
        table_2 = list(csv.DictReader(stream))
    with manifest.table_3_csv.open(encoding="utf-8", newline="") as stream:
        table_3 = list(csv.DictReader(stream))
    assert len(table_2) == 10
    assert len(table_3) == 9
    assert table_2[0]["Rank"] == "1"
    assert table_2[0]["Bankruptcy Rate"] == ""
    assert table_3[0]["Positive-System Seeds"] == "3/3"


@pytest.mark.parametrize("model_count", [2, 8, 9, 10])
def test_report_derives_all_dimensions_from_candidate_pool(
    model_count: int, tmp_path: Path
) -> None:
    analysis = _analysis(model_count)
    report = build_paper_report(analysis)
    manifest = write_paper_artifacts(report, tmp_path)

    assert report.model_count == model_count
    assert report.seed_count == 3
    assert len(report.performance) == model_count + 1
    assert len(report.replacements) == model_count
    assert len(report.externalities) == model_count * (model_count - 1)
    for performance in report.performance:
        assert len(performance.background_means_cents) == model_count
        assert analysis.track_a[performance.model_id].run_count == model_count * 3
    for replacement in report.replacements:
        assert replacement.mean_externality_per_firm_cents == pytest.approx(
            replacement.mean_others_difference_cents / (model_count - 1)
        )
    namespace = "{http://www.w3.org/2000/svg}"
    figure_2a = ET.parse(manifest.figure_2a_svg).getroot()
    assert len(figure_2a.findall(f"{namespace}circle")) == model_count * (model_count + 1)
    figure_3b = ET.parse(manifest.figure_3b_svg).getroot()
    assert f"{model_count}-by-{model_count}" in figure_3b.findtext(f"{namespace}desc", "")
    with manifest.table_2_csv.open(encoding="utf-8", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == model_count + 1
    with manifest.table_3_csv.open(encoding="utf-8", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == model_count


def test_report_rejects_missing_or_duplicate_matrix_cells() -> None:
    report = build_paper_report(_analysis(8))
    for cells in (report.externalities[:-1], (*report.externalities, report.externalities[0])):
        with pytest.raises(ValueError, match="complete off-diagonal matrix"):
            type(report).model_validate({**report.model_dump(), "externalities": cells})


def test_report_rejects_inconsistent_seed_set() -> None:
    analysis = _analysis(8)
    comparison = analysis.track_c[MODEL_IDS[0]]
    analysis.track_c[MODEL_IDS[0]] = comparison.model_copy(
        update={
            "seed_comparisons": (
                comparison.seed_comparisons[0].model_copy(update={"seed": 99}),
                *comparison.seed_comparisons[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="common paired seeds"):
        build_paper_report(analysis)


def test_report_rejects_wrong_background_count_in_rehydrated_report() -> None:
    report = build_paper_report(_analysis(8))
    malformed = report.performance[0].model_copy(
        update={"background_means_cents": report.performance[0].background_means_cents[:-1]}
    )
    with pytest.raises(ValueError, match="one mean per Leave-One-Out background"):
        type(report).model_validate(
            {**report.model_dump(), "performance": (malformed, *report.performance[1:])}
        )
