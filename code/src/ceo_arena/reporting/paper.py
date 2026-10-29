"""Build publication figures and tables from one frozen Arena analysis."""

from __future__ import annotations

import csv
import math
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, model_validator

from ceo_arena.evaluation import ArenaAnalysis
from ceo_arena.models import StrictModel
from ceo_arena.protocol import RULE_MODEL_ID

_USD_MILLION_CENTS = 100_000_000
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_INK = "#17202A"
_MUTED = "#667085"
_GRID = "#D8DEE8"
_GOLD = "#E5A50A"
_GOLD_DARK = "#8C6500"
_SLATE = "#667085"
_POSITIVE = (25, 118, 210)
_NEGATIVE = (213, 69, 56)
_NEUTRAL = (247, 248, 250)
_MODEL_COLORS = (
    "#1F77B4",
    "#E07A1F",
    "#2A9D8F",
    "#C44536",
    "#6C5CE7",
    "#8C6D31",
    "#D65DB1",
    "#526D82",
    "#5C940D",
)


class ModelPerformance(StrictModel):
    """One Table 2 row and the source data for both Figure 2 panels."""

    rank: int = Field(gt=0)
    model_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    background_means_cents: tuple[float, ...] = Field(min_length=2)
    mean_score_cents: float
    opponent_std_cents: float = Field(ge=0.0)
    seed_std_cents: float = Field(ge=0.0)
    bankruptcy_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class SeedReplacementPoint(StrictModel):
    """One same-seed Rule replacement point in Figure 3A."""

    model_id: str = Field(min_length=1)
    seed: int
    private_difference_cents: int
    system_difference_cents: int


class ReplacementSummary(StrictModel):
    """One model-level Table 3 row and mean point in Figure 3A."""

    model_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    seed_points: tuple[SeedReplacementPoint, ...] = Field(min_length=1)
    mean_private_difference_cents: float
    mean_externality_per_firm_cents: float
    mean_others_difference_cents: float
    mean_system_difference_cents: float
    positive_system_seed_count: int = Field(ge=0)


class DirectedExternality(StrictModel):
    """One off-diagonal cell of the directed Figure 3B matrix."""

    affected_model_id: str = Field(min_length=1)
    entrant_model_id: str = Field(min_length=1)
    mean_effect_cents: float
    seed_effects_cents: tuple[int, ...] = Field(min_length=1)
    direction_consistent: bool
    exceeds_threshold: bool

    @property
    def highlighted(self) -> bool:
        """Return whether the cell passes both pre-registered display rules."""

        return self.direction_consistent and self.exceeds_threshold


class PaperReportData(StrictModel):
    """Typed, render-ready data for the paper's two empirical figures and tables."""

    baseline_model_id: str = Field(min_length=1)
    performance: tuple[ModelPerformance, ...] = Field(min_length=3)
    replacements: tuple[ReplacementSummary, ...] = Field(min_length=2)
    externalities: tuple[DirectedExternality, ...] = Field(min_length=2)
    economic_threshold_cents: int | None = Field(default=None, gt=0)

    @property
    def model_count(self) -> int:
        """Return the non-Rule model count and fixed market size."""
        return len(self.performance) - 1

    @property
    def seed_count(self) -> int:
        """Return the common number of paired seed observations."""
        return len(self.replacements[0].seed_points)

    @model_validator(mode="after")
    def validate_complete_design(self) -> PaperReportData:
        model_ids = tuple(item.model_id for item in self.performance)
        if len(set(model_ids)) != len(model_ids) or self.baseline_model_id not in model_ids:
            raise ValueError("paper report requires distinct candidates and one Rule baseline")
        if any(len(item.background_means_cents) != self.model_count for item in self.performance):
            raise ValueError("each candidate requires one mean per Leave-One-Out background")
        replacement_ids = tuple(item.model_id for item in self.replacements)
        expected_replacements = set(model_ids) - {self.baseline_model_id}
        if (
            set(replacement_ids) != expected_replacements
            or len(replacement_ids) != self.model_count
        ):
            raise ValueError("paper report requires one replacement summary per non-Rule model")
        seeds = {point.seed for point in self.replacements[0].seed_points}
        if len(seeds) != self.seed_count or any(
            len(item.seed_points) != self.seed_count
            or {point.seed for point in item.seed_points} != seeds
            or any(point.model_id != item.model_id for point in item.seed_points)
            or item.positive_system_seed_count > self.seed_count
            for item in self.replacements
        ):
            raise ValueError("replacement summaries must contain the same distinct seeds")
        expected_cells = {
            (affected, entrant)
            for affected in expected_replacements
            for entrant in expected_replacements
            if affected != entrant
        }
        actual_cells = {
            (item.affected_model_id, item.entrant_model_id) for item in self.externalities
        }
        if actual_cells != expected_cells or len(self.externalities) != len(expected_cells):
            raise ValueError("directed externalities must form a complete off-diagonal matrix")
        if any(len(item.seed_effects_cents) != self.seed_count for item in self.externalities):
            raise ValueError("directed externalities must cover every paired seed")
        return self


class PaperArtifactManifest(StrictModel):
    """Paths written by :func:`write_paper_artifacts`."""

    figure_2a_svg: Path
    figure_2b_svg: Path
    figure_3a_svg: Path
    figure_3b_svg: Path
    table_2_csv: Path
    table_3_csv: Path


def build_paper_report(
    analysis: ArenaAnalysis,
    *,
    baseline_model_id: str = RULE_MODEL_ID,
    labels: Mapping[str, str] | None = None,
    bankruptcy_rates: Mapping[str, float] | None = None,
    economic_threshold_cents: int | None = None,
) -> PaperReportData:
    """Validate the final protocol and convert an Arena analysis into report data."""

    labels = labels or {}
    bankruptcy_rates = bankruptcy_rates or {}
    model_ids = set(analysis.track_a)
    if model_ids != set(analysis.track_b) or model_ids != set(analysis.health):
        raise ValueError("Track A, Track B, and health must cover the same candidates")
    if len(model_ids) < 3 or baseline_model_id not in model_ids:
        raise ValueError("paper reporting requires at least two models and one Rule baseline")
    if set(analysis.track_c) != model_ids - {baseline_model_id}:
        raise ValueError("Track C must cover every non-Rule model exactly once")
    unknown_bankruptcy_models = set(bankruptcy_rates) - model_ids
    if unknown_bankruptcy_models:
        raise ValueError(f"bankruptcy rates contain unknown models: {unknown_bankruptcy_models}")
    model_count = len(model_ids) - 1
    first_comparison = next(iter(analysis.track_c.values()))
    seed_ids = tuple(sorted(item.seed for item in first_comparison.seed_comparisons))
    if not seed_ids or len(set(seed_ids)) != len(seed_ids):
        raise ValueError("paper reporting requires distinct paired seeds")

    ranked_ids = tuple(
        sorted(
            model_ids,
            key=lambda model_id: (
                -analysis.track_a[model_id].mean_official_score_cents,
                model_id,
            ),
        )
    )
    performance = tuple(
        _build_performance(
            analysis,
            model_id,
            rank,
            labels.get(model_id, model_id),
            bankruptcy_rates.get(model_id),
            model_count,
            len(seed_ids),
        )
        for rank, model_id in enumerate(ranked_ids, start=1)
    )
    non_rule_ids = tuple(model_id for model_id in ranked_ids if model_id != baseline_model_id)
    replacements = tuple(
        _build_replacement(analysis, model_id, labels.get(model_id, model_id), seed_ids)
        for model_id in non_rule_ids
    )
    externalities = tuple(
        _build_externality(
            analysis,
            affected_model_id,
            entrant_model_id,
            economic_threshold_cents,
        )
        for affected_model_id in non_rule_ids
        for entrant_model_id in non_rule_ids
        if affected_model_id != entrant_model_id
    )
    return PaperReportData(
        baseline_model_id=baseline_model_id,
        performance=performance,
        replacements=replacements,
        externalities=externalities,
        economic_threshold_cents=economic_threshold_cents,
    )


def write_paper_artifacts(
    report: PaperReportData,
    output_dir: str | Path,
) -> PaperArtifactManifest:
    """Write deterministic SVG figures and UTF-8 CSV tables."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = PaperArtifactManifest(
        figure_2a_svg=destination / "figure_2a_performance.svg",
        figure_2b_svg=destination / "figure_2b_stability.svg",
        figure_3a_svg=destination / "figure_3a_private_system.svg",
        figure_3b_svg=destination / "figure_3b_directed_externalities.svg",
        table_2_csv=destination / "table_2_performance.csv",
        table_3_csv=destination / "table_3_externalities.csv",
    )
    _render_figure_2a(report, manifest.figure_2a_svg)
    _render_figure_2b(report, manifest.figure_2b_svg)
    _render_figure_3a(report, manifest.figure_3a_svg)
    _render_figure_3b(report, manifest.figure_3b_svg)
    _write_table_2(report, manifest.table_2_csv)
    _write_table_3(report, manifest.table_3_csv)
    return manifest


def _build_performance(
    analysis: ArenaAnalysis,
    model_id: str,
    rank: int,
    label: str,
    bankruptcy_rate: float | None,
    model_count: int,
    seed_count: int,
) -> ModelPerformance:
    track_a = analysis.track_a[model_id]
    track_b = analysis.track_b[model_id]
    background_means = tuple(item.mean_cents for item in track_b.backgrounds)
    if (
        track_a.run_count != model_count * seed_count
        or track_b.run_count != model_count * seed_count
        or track_b.background_count != model_count
        or len(background_means) != model_count
        or any(len(item.seed_scores_cents) != seed_count for item in track_b.backgrounds)
    ):
        raise ValueError(
            f"model {model_id!r} must contain {model_count} backgrounds and "
            f"{seed_count} seeds per background"
        )
    if not math.isclose(track_a.mean_official_score_cents, track_b.mean_cents):
        raise ValueError(f"Track A and Track B means disagree for model {model_id!r}")
    return ModelPerformance(
        rank=rank,
        model_id=model_id,
        label=label,
        background_means_cents=background_means,
        mean_score_cents=track_a.mean_official_score_cents,
        opponent_std_cents=track_b.between_background_std_cents,
        seed_std_cents=track_b.within_background_std_cents,
        bankruptcy_rate=bankruptcy_rate,
    )


def _build_replacement(
    analysis: ArenaAnalysis,
    model_id: str,
    label: str,
    seed_ids: tuple[int, ...],
) -> ReplacementSummary:
    comparison = analysis.track_c[model_id]
    affected_ids = set(analysis.track_c) - {model_id}
    if (
        comparison.pair_count != len(seed_ids)
        or tuple(sorted(item.seed for item in comparison.seed_comparisons)) != seed_ids
        or set(comparison.mean_directed_effects_cents) != affected_ids
    ):
        raise ValueError(
            f"Track C for model {model_id!r} must contain the common paired seeds "
            "and an effect on every other non-Rule model"
        )
    seed_points = tuple(
        SeedReplacementPoint(
            model_id=model_id,
            seed=item.seed,
            private_difference_cents=item.private_difference_cents,
            system_difference_cents=item.system_difference_cents,
        )
        for item in sorted(comparison.seed_comparisons, key=lambda item: item.seed)
    )
    return ReplacementSummary(
        model_id=model_id,
        label=label,
        seed_points=seed_points,
        mean_private_difference_cents=comparison.mean_private_difference_cents,
        mean_externality_per_firm_cents=comparison.mean_others_difference_cents / len(affected_ids),
        mean_others_difference_cents=comparison.mean_others_difference_cents,
        mean_system_difference_cents=comparison.mean_system_difference_cents,
        positive_system_seed_count=comparison.positive_system_seed_count,
    )


def _build_externality(
    analysis: ArenaAnalysis,
    affected_model_id: str,
    entrant_model_id: str,
    economic_threshold_cents: int | None,
) -> DirectedExternality:
    comparison = analysis.track_c[entrant_model_id]
    try:
        seed_effects = tuple(
            item.directed_effects_cents[affected_model_id]
            for item in sorted(comparison.seed_comparisons, key=lambda item: item.seed)
        )
        mean_effect = comparison.mean_directed_effects_cents[affected_model_id]
    except KeyError as error:
        raise ValueError(
            f"Track C entrant {entrant_model_id!r} lacks effect on {affected_model_id!r}"
        ) from error
    signs = {_sign(value) for value in seed_effects}
    direction_consistent = 0 not in signs and len(signs) == 1
    return DirectedExternality(
        affected_model_id=affected_model_id,
        entrant_model_id=entrant_model_id,
        mean_effect_cents=mean_effect,
        seed_effects_cents=seed_effects,
        direction_consistent=direction_consistent,
        exceeds_threshold=(
            economic_threshold_cents is not None and abs(mean_effect) >= economic_threshold_cents
        ),
    )


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _write_table_2(report: PaperReportData, path: Path) -> None:
    headers = (
        "Rank",
        "CEO",
        "Mean Score (USD M)",
        "Opponent SD (USD M)",
        "Seed SD (USD M)",
        "Bankruptcy Rate",
    )
    rows = (
        (
            item.rank,
            item.label,
            _millions(item.mean_score_cents),
            _millions(item.opponent_std_cents),
            _millions(item.seed_std_cents),
            "" if item.bankruptcy_rate is None else f"{item.bankruptcy_rate:.1%}",
        )
        for item in report.performance
    )
    _write_csv(path, headers, rows)


def _write_table_3(report: PaperReportData, path: Path) -> None:
    headers = (
        "Model",
        "Delta Private (USD M)",
        "Mean Externality per Firm (USD M)",
        "Delta Others (USD M)",
        "Delta System (USD M)",
        "Positive-System Seeds",
    )
    rows = (
        (
            item.label,
            _millions(item.mean_private_difference_cents),
            _millions(item.mean_externality_per_firm_cents),
            _millions(item.mean_others_difference_cents),
            _millions(item.mean_system_difference_cents),
            f"{item.positive_system_seed_count}/{report.seed_count}",
        )
        for item in report.replacements
    )
    _write_csv(path, headers, rows)


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def _millions(cents: float) -> str:
    return f"{cents / _USD_MILLION_CENTS:.3f}"


@dataclass(frozen=True, slots=True)
class _LinearScale:
    domain_min: float
    domain_max: float
    range_min: float
    range_max: float

    def __call__(self, value: float) -> float:
        ratio = (value - self.domain_min) / (self.domain_max - self.domain_min)
        return self.range_min + ratio * (self.range_max - self.range_min)


class _SvgCanvas:
    """Tiny SVG builder shared by all paper figures."""

    def __init__(self, width: int, height: int, title: str, description: str) -> None:
        ET.register_namespace("", _SVG_NAMESPACE)
        self.width = width
        self.height = height
        self.root = ET.Element(
            "svg",
            {
                "xmlns": _SVG_NAMESPACE,
                "viewBox": f"0 0 {width} {height}",
                "role": "img",
                "aria-labelledby": "figure-title figure-description",
            },
        )
        ET.SubElement(self.root, "title", {"id": "figure-title"}).text = title
        ET.SubElement(self.root, "desc", {"id": "figure-description"}).text = description
        style = ET.SubElement(self.root, "style")
        style.text = (
            "text{font-family:Inter,Arial,sans-serif;fill:#17202A}"
            ".title{font-size:25px;font-weight:700}"
            ".subtitle{font-size:14px;fill:#667085}"
            ".label{font-size:14px;font-weight:600}"
            ".tick{font-size:12px;fill:#667085}"
            ".note{font-size:12px;fill:#667085}"
        )
        self.rect(0, 0, width, height, fill="#FFFFFF")

    def element(self, tag: str, **attributes: object) -> ET.Element:
        return ET.SubElement(
            self.root,
            tag,
            {key.replace("_", "-"): str(value) for key, value in attributes.items()},
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, **attributes: object) -> None:
        self.element("line", x1=x1, y1=y1, x2=x2, y2=y2, **attributes)

    def rect(self, x: float, y: float, width: float, height: float, **attributes: object) -> None:
        self.element("rect", x=x, y=y, width=width, height=height, **attributes)

    def circle(self, cx: float, cy: float, radius: float, **attributes: object) -> None:
        self.element("circle", cx=cx, cy=cy, r=radius, **attributes)

    def polygon(self, points: Sequence[tuple[float, float]], **attributes: object) -> None:
        serialized = " ".join(f"{x},{y}" for x, y in points)
        self.element("polygon", points=serialized, **attributes)

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        css_class: str | None = None,
        **attributes: object,
    ) -> None:
        if css_class is not None:
            attributes["class"] = css_class
        self.element("text", x=x, y=y, **attributes).text = value

    def save(self, path: Path) -> None:
        ET.indent(self.root, space="  ")
        ET.ElementTree(self.root).write(path, encoding="utf-8", xml_declaration=True)


def _render_figure_2a(report: PaperReportData, path: Path) -> None:
    width, height = 1280, 820
    left, right, top, bottom = 310, 70, 125, 100
    canvas = _SvgCanvas(
        width,
        height,
        "Average operating performance across opponent compositions",
        f"{report.model_count} lineup means per candidate, averaged over {report.seed_count} "
        "seeds, overall means, and opponent-composition SD.",
    )
    canvas.text(
        left,
        42,
        "Average operating performance across opponent compositions",
        css_class="title",
    )
    canvas.text(
        left,
        68,
        f"Each small point is one lineup averaged over {report.seed_count} seeds; "
        "diamonds show Track A means.",
        css_class="subtitle",
    )
    values = [
        value
        for item in report.performance
        for value in (
            *item.background_means_cents,
            item.mean_score_cents - item.opponent_std_cents,
            item.mean_score_cents + item.opponent_std_cents,
        )
    ]
    scale, ticks = _money_scale(values, left, width - right, include_zero=True)
    _draw_x_axis(canvas, scale, ticks, top, height - bottom, "Official Score (USD millions)")
    row_gap = (height - bottom - top) / len(report.performance)
    for index, item in enumerate(report.performance):
        y = top + (index + 0.5) * row_gap
        canvas.text(left - 18, y + 5, item.label, css_class="label", text_anchor="end")
        canvas.line(
            scale(item.mean_score_cents - item.opponent_std_cents),
            y,
            scale(item.mean_score_cents + item.opponent_std_cents),
            y,
            stroke=_GOLD_DARK,
            stroke_width=5,
            stroke_linecap="round",
            opacity=0.72,
        )
        for point_index, value in enumerate(item.background_means_cents):
            offset = ((point_index % 3) - 1) * 4
            canvas.circle(
                scale(value),
                y + offset,
                4.2,
                fill="#FFFFFF",
                stroke=_SLATE,
                stroke_width=1.6,
                opacity=0.9,
            )
        mean_x = scale(item.mean_score_cents)
        canvas.polygon(
            ((mean_x, y - 8), (mean_x + 8, y), (mean_x, y + 8), (mean_x - 8, y)),
            fill=_GOLD,
            stroke=_INK,
            stroke_width=1,
        )
    canvas.text(
        left,
        height - 25,
        "Horizontal bars: mean ± one opponent-composition SD "
        "(descriptive, not a confidence interval).",
        css_class="note",
    )
    canvas.save(path)


def _render_figure_2b(report: PaperReportData, path: Path) -> None:
    width, height = 1280, 820
    left, right, top, bottom = 310, 70, 125, 100
    canvas = _SvgCanvas(
        width,
        height,
        "Opponent-composition variation versus seed variation",
        "A two-point comparison of between-lineup and within-lineup standard deviations.",
    )
    canvas.text(left, 42, "Opponent-composition variation versus seed variation", css_class="title")
    canvas.text(
        left,
        68,
        "Two descriptive sources of Score variation; connected points belong "
        "to the same candidate.",
        css_class="subtitle",
    )
    values = [
        value
        for item in report.performance
        for value in (item.opponent_std_cents, item.seed_std_cents)
    ]
    scale, ticks = _money_scale((0.0, *values), left, width - right, include_zero=True)
    _draw_x_axis(
        canvas,
        scale,
        ticks,
        top,
        height - bottom,
        "Score standard deviation (USD millions)",
    )
    row_gap = (height - bottom - top) / len(report.performance)
    for index, item in enumerate(report.performance):
        y = top + (index + 0.5) * row_gap
        opponent_x = scale(item.opponent_std_cents)
        seed_x = scale(item.seed_std_cents)
        canvas.text(left - 18, y + 5, item.label, css_class="label", text_anchor="end")
        canvas.line(opponent_x, y, seed_x, y, stroke="#AAB2BF", stroke_width=2)
        canvas.circle(opponent_x, y, 7, fill=_GOLD, stroke=_INK, stroke_width=1)
        canvas.polygon(
            ((seed_x, y - 8), (seed_x + 8, y + 7), (seed_x - 8, y + 7)),
            fill=_SLATE,
            stroke=_INK,
            stroke_width=1,
        )
    legend_y = 94
    canvas.circle(left, legend_y, 6, fill=_GOLD, stroke=_INK, stroke_width=1)
    canvas.text(left + 14, legend_y + 4, "Opponent-composition SD", css_class="tick")
    triangle_x = left + 220
    canvas.polygon(
        (
            (triangle_x, legend_y - 7),
            (triangle_x + 7, legend_y + 6),
            (triangle_x - 7, legend_y + 6),
        ),
        fill=_SLATE,
        stroke=_INK,
        stroke_width=1,
    )
    canvas.text(triangle_x + 14, legend_y + 4, "Within-lineup seed SD", css_class="tick")
    canvas.save(path)


def _render_figure_3a(report: PaperReportData, path: Path) -> None:
    width, height = 1040, 920
    left, right, top, bottom = 150, 90, 110, 120
    canvas = _SvgCanvas(
        width,
        height,
        "Private gains and total-system gains from replacing Rule",
        f"Same-seed Rule replacement effects for {report.model_count} models, with model means.",
    )
    canvas.text(
        left,
        42,
        "Private gains and total-system gains from replacing Rule",
        css_class="title",
    )
    canvas.text(
        left,
        68,
        "Small points are seeds; labeled points are model means. "
        "The diagonal implies zero net effect on others.",
        css_class="subtitle",
    )
    all_values = [
        value
        for item in report.replacements
        for point in item.seed_points
        for value in (point.private_difference_cents, point.system_difference_cents)
    ]
    all_values.extend(
        value
        for item in report.replacements
        for value in (item.mean_private_difference_cents, item.mean_system_difference_cents)
    )
    limit = max(max((abs(value) for value in all_values), default=0.0) * 1.12, 1.0)
    x_scale = _LinearScale(-limit, limit, left, width - right)
    y_scale = _LinearScale(-limit, limit, height - bottom, top)
    ticks = _symmetric_ticks(limit)
    _draw_xy_axes(canvas, x_scale, y_scale, ticks, left, width - right, top, height - bottom)
    canvas.line(
        x_scale(-limit),
        y_scale(-limit),
        x_scale(limit),
        y_scale(limit),
        stroke="#98A2B3",
        stroke_width=1.5,
        stroke_dasharray="6 6",
    )
    for index, item in enumerate(report.replacements):
        color = _MODEL_COLORS[index % len(_MODEL_COLORS)]
        for point in item.seed_points:
            canvas.circle(
                x_scale(point.private_difference_cents),
                y_scale(point.system_difference_cents),
                4.5,
                fill=color,
                opacity=0.38,
            )
        x = x_scale(item.mean_private_difference_cents)
        y = y_scale(item.mean_system_difference_cents)
        canvas.circle(x, y, 8, fill=color, stroke="#FFFFFF", stroke_width=2)
        canvas.text(x + 11, y - 9, _short_label(item.label), css_class="tick", font_weight=700)
    canvas.text(
        (left + width - right) / 2,
        height - 40,
        "Private Score change relative to Rule (USD millions)",
        css_class="label",
        text_anchor="middle",
    )
    canvas.text(
        35,
        (top + height - bottom) / 2,
        "System Score change (USD millions)",
        css_class="label",
        text_anchor="middle",
        transform=f"rotate(-90 35 {(top + height - bottom) / 2})",
    )
    canvas.save(path)


def _render_figure_3b(report: PaperReportData, path: Path) -> None:
    width, height = 1220, 1100
    left, right, top, bottom = 280, 130, 230, 155
    canvas = _SvgCanvas(
        width,
        height,
        "Directed payoff externalities relative to Rule",
        f"A {report.model_count}-by-{report.model_count} matrix where rows are affected "
        "models and columns are "
        "models entering in place of Rule.",
    )
    canvas.text(left, 42, "Directed payoff externalities relative to Rule", css_class="title")
    canvas.text(
        left,
        68,
        "Cell (A, B): change in A's Score when B replaces Rule; diagonal cells are undefined.",
        css_class="subtitle",
    )
    model_ids = tuple(item.model_id for item in report.replacements)
    labels = {item.model_id: item.label for item in report.replacements}
    effects = {
        (item.affected_model_id, item.entrant_model_id): item for item in report.externalities
    }
    cell = min((width - left - right), (height - top - bottom)) / report.model_count
    matrix_width = cell * report.model_count
    max_effect = max((abs(item.mean_effect_cents) for item in report.externalities), default=1.0)
    max_effect = max(max_effect, 1.0)
    for row, affected in enumerate(model_ids):
        y = top + row * cell
        canvas.text(
            left - 18,
            y + cell / 2 + 5,
            _short_label(labels[affected]),
            css_class="label",
            text_anchor="end",
        )
        for column, entrant in enumerate(model_ids):
            x = left + column * cell
            if affected == entrant:
                canvas.rect(x, y, cell, cell, fill="#ECEFF3", stroke="#FFFFFF", stroke_width=2)
                canvas.line(x + 12, y + 12, x + cell - 12, y + cell - 12, stroke="#B7C0CD")
                continue
            externality = effects[(affected, entrant)]
            color = _diverging_color(externality.mean_effect_cents / max_effect)
            canvas.rect(x, y, cell, cell, fill=color, stroke="#FFFFFF", stroke_width=2)
            canvas.text(
                x + cell / 2,
                y + cell / 2 + 4,
                _compact_millions(externality.mean_effect_cents),
                css_class="tick",
                text_anchor="middle",
                font_weight=700,
            )
            if externality.highlighted:
                canvas.text(x + cell - 9, y + 15, "*", css_class="label", text_anchor="end")
    for column, entrant in enumerate(model_ids):
        x = left + (column + 0.5) * cell
        canvas.text(
            x,
            top - 14,
            _short_label(labels[entrant]),
            css_class="label",
            text_anchor="start",
            transform=f"rotate(-45 {x} {top - 14})",
        )
    canvas.text(
        left + matrix_width / 2,
        height - 72,
        "Model entering in place of Rule (B)",
        css_class="label",
        text_anchor="middle",
    )
    canvas.text(
        42,
        top + matrix_width / 2,
        "Affected model (A)",
        css_class="label",
        text_anchor="middle",
        transform=f"rotate(-90 42 {top + matrix_width / 2})",
    )
    legend_x = left + matrix_width + 34
    legend_y = top
    legend_height = matrix_width
    steps = 60
    for step in range(steps):
        ratio = 1 - 2 * step / (steps - 1)
        canvas.rect(
            legend_x,
            legend_y + step * legend_height / steps,
            20,
            legend_height / steps + 1,
            fill=_diverging_color(ratio),
        )
    canvas.text(legend_x + 29, legend_y + 5, f"+{_compact_millions(max_effect)}", css_class="tick")
    canvas.text(legend_x + 29, legend_y + legend_height / 2 + 4, "0", css_class="tick")
    canvas.text(
        legend_x + 29,
        legend_y + legend_height,
        f"-{_compact_millions(max_effect)}",
        css_class="tick",
    )
    threshold_note = (
        f"* Same direction in all {report.seed_count} seeds and |mean effect| meets the "
        "pre-registered threshold."
        if report.economic_threshold_cents is not None
        else "No economic threshold configured; cells are intentionally unstarred."
    )
    canvas.text(left, height - 30, threshold_note, css_class="note")
    canvas.save(path)


def _money_scale(
    values: Iterable[float],
    range_min: float,
    range_max: float,
    *,
    include_zero: bool,
) -> tuple[_LinearScale, tuple[float, ...]]:
    raw = tuple(values)
    minimum = min(raw, default=0.0)
    maximum = max(raw, default=0.0)
    if include_zero:
        minimum = min(minimum, 0.0)
        maximum = max(maximum, 0.0)
    if math.isclose(minimum, maximum):
        padding = max(abs(minimum) * 0.1, float(_USD_MILLION_CENTS))
    else:
        padding = (maximum - minimum) * 0.08
    minimum -= padding
    maximum += padding
    ticks = _ticks(minimum, maximum)
    return _LinearScale(minimum, maximum, range_min, range_max), ticks


def _ticks(minimum: float, maximum: float, count: int = 5) -> tuple[float, ...]:
    span = maximum - minimum
    rough_step = span / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(rough_step))
    normalized = rough_step / magnitude
    nice = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    step = nice * magnitude
    start = math.ceil(minimum / step) * step
    end = math.floor(maximum / step) * step
    return tuple(start + index * step for index in range(round((end - start) / step) + 1))


def _symmetric_ticks(limit: float) -> tuple[float, ...]:
    return (-limit, -limit / 2, 0.0, limit / 2, limit)


def _draw_x_axis(
    canvas: _SvgCanvas,
    scale: _LinearScale,
    ticks: Sequence[float],
    top: float,
    bottom: float,
    label: str,
) -> None:
    for tick in ticks:
        x = scale(tick)
        canvas.line(x, top, x, bottom, stroke=_GRID, stroke_width=1)
        canvas.text(x, bottom + 24, _compact_millions(tick), css_class="tick", text_anchor="middle")
    if scale.domain_min <= 0 <= scale.domain_max:
        x_zero = scale(0)
        canvas.line(x_zero, top, x_zero, bottom, stroke="#98A2B3", stroke_width=1.5)
    canvas.text(
        (scale.range_min + scale.range_max) / 2,
        bottom + 58,
        label,
        css_class="label",
        text_anchor="middle",
    )


def _draw_xy_axes(
    canvas: _SvgCanvas,
    x_scale: _LinearScale,
    y_scale: _LinearScale,
    ticks: Sequence[float],
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> None:
    for tick in ticks:
        x = x_scale(tick)
        y = y_scale(tick)
        canvas.line(x, top, x, bottom, stroke=_GRID, stroke_width=1)
        canvas.line(left, y, right, y, stroke=_GRID, stroke_width=1)
        canvas.text(x, bottom + 24, _compact_millions(tick), css_class="tick", text_anchor="middle")
        canvas.text(left - 13, y + 4, _compact_millions(tick), css_class="tick", text_anchor="end")
    canvas.line(x_scale(0), top, x_scale(0), bottom, stroke="#98A2B3", stroke_width=1.5)
    canvas.line(left, y_scale(0), right, y_scale(0), stroke="#98A2B3", stroke_width=1.5)


def _compact_millions(cents: float) -> str:
    value = cents / _USD_MILLION_CENTS
    if math.isclose(value, 0.0, abs_tol=5e-7):
        return "0"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _short_label(label: str, limit: int = 18) -> str:
    return label if len(label) <= limit else f"{label[: limit - 1]}…"


def _diverging_color(value: float) -> str:
    clipped = max(-1.0, min(1.0, value))
    target = _POSITIVE if clipped >= 0 else _NEGATIVE
    weight = abs(clipped)
    rgb = tuple(
        round(neutral + (channel - neutral) * weight)
        for neutral, channel in zip(_NEUTRAL, target, strict=True)
    )
    return "#" + "".join(f"{channel:02X}" for channel in rgb)
