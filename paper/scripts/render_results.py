"""Render the fixed paper panels from a complete, audited CEO Arena analysis."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import matplotlib as mpl
import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties, findfont
from matplotlib.lines import Line2D
from matplotlib.text import Text
from matplotlib.ticker import AutoMinorLocator, MaxNLocator, MultipleLocator
from pydantic import TypeAdapter

from ceo_arena.evaluation import ArenaAnalysis
from ceo_arena.reporting import PaperReportData, build_paper_report

LABELS = {
    "gpt-6-astra": "GPT-6 Astra",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "claude-sonnet-5": "Claude Sonnet 5",
    "qwen3.8-max": "Qwen 3.8 Max",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "doubao-seed-1.8": "Doubao Seed 1.8",
    "deterministic-rule-ceo": "Rule",
}
SHORT_LABELS = {
    "gpt-6-astra": "Astra",
    "deepseek-v4-pro": "DS V4 Pro",
    "claude-sonnet-5": "Sonnet 5",
    "qwen3.8-max": "Qwen 3.8",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gemini-3.8-flash": "Gemini 3.8",
    "deepseek-v4-flash": "DS V4 Flash",
    "doubao-seed-1.8": "Doubao 1.8",
    "deterministic-rule-ceo": "Rule",
}
COLORS = (
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
    "#332288",
    "#44AA99",
    "#882255",
    "#667085",
)
CENTS_PER_THOUSAND_USD = 100_000
TEXT_WIDTH_INCHES = 5.5
PANEL_HEIGHT_INCHES = 3.75
MINIMUM_FONT_SIZE = 9
SEED_MARKERS = ("o", "^", "s")


class FigureLanguage:
    """Keep figure labels and font selection together for both editions."""

    def __init__(self, language: Literal["en", "zh"]) -> None:
        self.language = language
        self.font = "DejaVu Sans"
        if language == "zh":
            self.font = FontProperties(
                fname=findfont(FontProperties(family="Microsoft YaHei"), fallback_to_default=False)
            ).get_name()

    def text(self, english: str, chinese: str) -> str:
        return chinese if self.language == "zh" else english

    @property
    def score(self) -> str:
        return self.text("Score", "公司得分")

    @property
    def mean_score(self) -> str:
        return self.text("Mean Score", "平均得分")

    @property
    def lineup_mean_score(self) -> str:
        return self.text("Lineup-mean Score", "阵容平均得分")

    @property
    def private_difference(self) -> str:
        return self.text("Private Score difference", "自身得分差")

    @property
    def market_difference(self) -> str:
        return self.text("Market Score difference", "市场总得分差")

    @property
    def mean_directed_difference(self) -> str:
        return self.text("Mean directed Score difference", "平均定向得分差")

    @property
    def opponent_sd(self) -> str:
        return self.text("opponent-composition SD", "对手阵容标准差")

    @property
    def seed_sd(self) -> str:
        return self.text("within-lineup seed SD", "同阵容跨种子标准差")

    def money_label(self, metric: str, *, wrap: bool = False) -> str:
        """Attach monetary units without abbreviating the metric name."""
        label = metric.replace("Score difference", "Score\ndifference") if wrap else metric
        return label + self.text(" (USD k)", " (千美元)")


def latex_text(value: str) -> str:
    """Escape a label without re-escaping replacement commands."""
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


class PaperResultRenderer:
    """Keep monetary conversion, model order, and figure/table export consistent."""

    def __init__(self, report: PaperReportData, language: Literal["en", "zh"] = "en") -> None:
        self.report = report
        self.language = FigureLanguage(language)
        self.model_order = tuple(item.model_id for item in report.replacements)
        self.color_by_model = dict(
            zip(self.model_order, COLORS[: len(self.model_order)], strict=True)
        )

    @staticmethod
    def _money(cents: float) -> float:
        return cents / CENTS_PER_THOUSAND_USD

    @staticmethod
    def _short(model_id: str) -> str:
        return SHORT_LABELS.get(model_id, model_id)

    def _title(self, figure: Figure, english: str, chinese: str) -> None:
        figure.suptitle(
            self.language.text(english, chinese), x=0.02, y=0.98, ha="left", weight="bold"
        )

    def _model_rows(self, axes: Axes, *, show_labels: bool) -> None:
        """Align the same model rows exactly across the performance panels."""
        count = len(self.report.performance)
        labels = (
            [self._short(item.model_id) for item in self.report.performance]
            if show_labels
            else [""] * count
        )
        axes.set_yticks(range(count), labels)
        axes.tick_params(axis="y", left=show_labels, labelleft=show_labels)
        axes.set_ylim(count - 0.5, -0.5)

    @staticmethod
    def _horizontal_scale(axes: Axes, major_step: float, *, rotate_labels: bool = False) -> None:
        """Add labeled major ticks and lighter half-step guides."""
        axes.xaxis.set_major_locator(MultipleLocator(major_step))
        axes.xaxis.set_minor_locator(AutoMinorLocator(2))
        axes.grid(axis="x", which="minor", color="#EEF0F3", linewidth=0.5, linestyle=":")
        if rotate_labels:
            axes.tick_params(axis="x", labelrotation=45)
            for label in axes.get_xticklabels():
                label.set_horizontalalignment("right")

    @staticmethod
    def _canvas(
        width_fraction: float, bounds: tuple[float, float, float, float]
    ) -> tuple[Figure, Axes]:
        figure = Figure(figsize=(TEXT_WIDTH_INCHES * width_fraction, PANEL_HEIGHT_INCHES))
        FigureCanvasAgg(figure)
        axes = figure.add_axes(bounds)
        axes.spines[["top", "right"]].set_visible(False)
        axes.grid(axis="x", color="#E4E7EC", linewidth=0.7)
        axes.set_axisbelow(True)
        axes.xaxis.set_major_locator(MaxNLocator(3))
        axes.yaxis.set_major_locator(MaxNLocator(3))
        return figure, axes

    @staticmethod
    def _save(figure: Figure, path: Path) -> None:
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        texts = list(figure.texts)
        for axes in figure.axes:
            texts.extend((axes.title, axes.xaxis.label, axes.yaxis.label, *axes.texts))
            for axis, limits in ((axes.xaxis, axes.get_xlim()), (axes.yaxis, axes.get_ylim())):
                lower, upper = sorted(limits)
                for tick in axis.get_major_ticks():
                    if lower <= tick.get_loc() <= upper:
                        texts.extend((tick.label1, tick.label2))
            legend = axes.get_legend()
            if legend is not None:
                texts.extend(legend.get_texts())
        for legend in figure.legends:
            texts.extend(legend.get_texts())
            for axes in figure.axes:
                if legend.get_window_extent(renderer).overlaps(
                    axes.xaxis.label.get_window_extent(renderer)
                ):
                    raise ValueError(f"Legend overlaps the axis label in {path.name}")
        for text in texts:
            if not isinstance(text, Text) or not text.get_visible() or not text.get_text():
                continue
            if text.get_fontsize() < MINIMUM_FONT_SIZE:
                raise ValueError(f"Figure text is below {MINIMUM_FONT_SIZE} pt: {text.get_text()}")
            bounds = text.get_window_extent(renderer)
            if (
                bounds.x0 < 0
                or bounds.y0 < 0
                or bounds.x1 > figure.bbox.x1
                or bounds.y1 > figure.bbox.y1
            ):
                raise ValueError(f"Figure text is clipped in {path.name}: {text.get_text()}")
        figure.savefig(path, format="pdf", metadata={"Creator": "CEO Arena paper renderer"})

    def _performance(self, directory: Path) -> None:
        figure, axes = self._canvas(0.57, (0.29, 0.32, 0.68, 0.58))
        for row, item in enumerate(self.report.performance):
            mean = self._money(item.mean_score_cents)
            spread = self._money(item.opponent_std_cents)
            axes.plot([mean - spread, mean + spread], [row, row], color="#B58A00", lw=2.0)
            axes.scatter(
                [self._money(score) for score in item.background_means_cents],
                row + np.linspace(-0.11, 0.11, len(item.background_means_cents)),
                s=18,
                color="#65758B",
                zorder=3,
            )
            axes.scatter(
                mean,
                row,
                marker="D",
                s=32,
                color="#D4A600",
                edgecolor="black",
                linewidth=0.4,
                zorder=4,
            )
        self._model_rows(axes, show_labels=True)
        axes.axvline(0, color="#8D99A8", linestyle=":", linewidth=0.8)
        self._horizontal_scale(axes, 100, rotate_labels=True)
        axes.set_xlabel(self.language.money_label(self.language.score))
        self._title(figure, "A  Mean Score", "A  平均得分")
        figure.legend(
            handles=[
                Line2D(
                    [],
                    [],
                    color="#65758B",
                    marker="o",
                    linestyle="none",
                    markersize=3,
                    label=self.language.lineup_mean_score,
                ),
                Line2D(
                    [],
                    [],
                    color="#B58A00",
                    marker="D",
                    markersize=4,
                    label=f"{self.language.mean_score} ±\n{self.language.opponent_sd}",
                ),
            ],
            loc="lower center",
            bbox_to_anchor=(0.5, 0.015),
            ncols=1,
            frameon=False,
            borderpad=0,
            borderaxespad=0,
            labelspacing=0.35,
            handlelength=1.0,
            columnspacing=0.8,
            handletextpad=0.4,
        )
        self._save(figure, directory / "figure_2a.pdf")

    def _stability(self, directory: Path) -> None:
        figure, axes = self._canvas(0.41, (0.08, 0.32, 0.89, 0.58))
        for row, item in enumerate(self.report.performance):
            opponent = self._money(item.opponent_std_cents)
            seed = self._money(item.seed_std_cents)
            axes.plot([opponent, seed], [row, row], color="#A7B0BE", lw=1.2)
            axes.scatter(
                opponent,
                row,
                color="#187F85",
                s=27,
                label=self.language.opponent_sd if row == 0 else None,
            )
            axes.scatter(
                seed,
                row,
                color="#8960A8",
                marker="^",
                s=30,
                label=self.language.seed_sd if row == 0 else None,
            )
        self._model_rows(axes, show_labels=False)
        axes.grid(axis="y", color="#EEF0F3", linewidth=0.5)
        axes.set_xlim(left=0)
        self._horizontal_scale(axes, 25)
        axes.set_xlabel(self.language.text("SD (USD k)", "标准差 (千美元)"))
        self._title(figure, "B  Observed spread", "B  观测离散程度")
        figure.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, 0.02),
            ncols=1,
            frameon=False,
            borderpad=0,
            borderaxespad=0,
            labelspacing=0.35,
            handlelength=0.7,
            columnspacing=0.65,
            handletextpad=0.35,
        )
        self._save(figure, directory / "figure_2b.pdf")

    def _replacement(self, directory: Path) -> None:
        figure, axes = self._canvas(0.49, (0.34, 0.36, 0.625, 0.54))
        x_coordinates, y_coordinates = [0.0], [0.0]
        model_handles = []
        for number, item in enumerate(self.report.replacements, start=1):
            color = self.color_by_model[item.model_id]
            x_values = [self._money(point.private_difference_cents) for point in item.seed_points]
            y_values = [self._money(point.system_difference_cents) for point in item.seed_points]
            x_coordinates.extend(x_values)
            y_coordinates.extend(y_values)
            for x_value, y_value, marker in zip(x_values, y_values, SEED_MARKERS, strict=True):
                axes.scatter(
                    x_value,
                    y_value,
                    s=21,
                    marker=marker,
                    color=color,
                    alpha=0.45,
                    edgecolor="none",
                    zorder=3,
                )
            mean_x = self._money(item.mean_private_difference_cents)
            mean_y = self._money(item.mean_system_difference_cents)
            axes.scatter(
                mean_x,
                mean_y,
                marker="D",
                s=36,
                color=color,
                edgecolor="none",
                linewidth=0,
                zorder=4,
            )
            axes.annotate(
                str(number),
                (mean_x, mean_y),
                xytext=(4, 4),
                textcoords="offset points",
                weight="bold",
                color=color,
                zorder=5,
            )
            model_handles.append(
                Line2D(
                    [],
                    [],
                    color=color,
                    marker="D",
                    linestyle="none",
                    markersize=4,
                    label=f"{number} {self._short(item.model_id)}",
                )
            )
        coordinates = (*x_coordinates, *y_coordinates)
        padding = max(np.ptp(coordinates) * 0.08, 1)
        limits = (min(coordinates) - padding, max(coordinates) + padding)
        ticks = MaxNLocator(5, steps=(1, 2, 2.5, 5, 10)).tick_values(*limits)
        shared_ticks = [tick for tick in ticks if limits[0] <= tick <= limits[1]]
        axes.plot(limits, limits, linestyle="--", color="#777777", linewidth=0.9)
        axes.axhline(0, color="#B3BAC5", linewidth=0.7)
        axes.axvline(0, color="#B3BAC5", linewidth=0.7)
        axes.set(xlim=limits, ylim=limits)
        axes.set_xticks(shared_ticks)
        axes.set_yticks(shared_ticks)
        axes.set_aspect("equal", adjustable="box", anchor="N")
        axes.xaxis.set_minor_locator(AutoMinorLocator(2))
        axes.yaxis.set_minor_locator(AutoMinorLocator(2))
        axes.grid(axis="both", which="major", color="#E4E7EC", linewidth=0.7)
        axes.set_xlabel(
            self.language.money_label(self.language.private_difference, wrap=True)
        )
        axes.set_ylabel(
            self.language.money_label(self.language.market_difference, wrap=True),
            labelpad=1,
        )
        self._title(figure, "A  Score differences", "A  得分差")
        axes.legend(
            handles=[
                Line2D(
                    [],
                    [],
                    color="#667085",
                    marker=marker,
                    linestyle="none",
                    markersize=3,
                    label=str(point.seed),
                )
                for point, marker in zip(
                    self.report.replacements[0].seed_points, SEED_MARKERS, strict=True
                )
            ],
            loc="upper left",
            ncols=3,
            frameon=False,
            handlelength=0.6,
            columnspacing=0.5,
            handletextpad=0.2,
            borderpad=0.3,
        )
        figure.legend(
            handles=model_handles,
            loc="lower center",
            bbox_to_anchor=(0.5, 0.005),
            ncols=2,
            frameon=False,
            handlelength=0.6,
            columnspacing=0.7,
            handletextpad=0.35,
            labelspacing=0.3,
        )
        self._save(figure, directory / "figure_3a.pdf")

    def _externalities(self, directory: Path) -> None:
        figure, axes = self._canvas(0.49, (0.15, 0.30, 0.84, 0.60))
        model_count = len(self.model_order)
        matrix = np.full((model_count, model_count), np.nan)
        positions = {model: index for index, model in enumerate(self.model_order)}
        for cell in self.report.externalities:
            matrix[positions[cell.affected_model_id], positions[cell.entrant_model_id]] = (
                self._money(cell.mean_effect_cents)
            )
        magnitude = max(float(np.nanmax(np.abs(matrix))), 1.0)
        palette = mpl.colormaps["RdBu"].copy()
        palette.set_bad("#D9DDE3")
        image = axes.imshow(matrix, cmap=palette, vmin=-magnitude, vmax=magnitude)
        labels = list(range(1, model_count + 1))
        axes.set_xticks(range(model_count), labels)
        axes.set_yticks(range(model_count), labels)
        axes.grid(False)
        axes.set_xlabel(self.language.text("Entrant replacing Rule", "替代 Rule 的进入模型"))
        axes.set_ylabel(self.language.text("Affected model", "受影响模型"))
        self._title(figure, "B  Directed Score differences", "B  定向得分差")
        color_axes = figure.add_axes((0.02, 0.14, 0.96, 0.045))
        colorbar = figure.colorbar(image, cax=color_axes, orientation="horizontal")
        colorbar.set_label(
            self.language.money_label(self.language.mean_directed_difference)
        )
        ticks = MaxNLocator(8, steps=(1, 2, 2.5, 5, 10)).tick_values(-magnitude, magnitude)
        colorbar.set_ticks([tick for tick in ticks if -magnitude <= tick <= magnitude])
        self._save(figure, directory / "figure_3b.pdf")

    def _tables(self, directory: Path) -> None:
        performance_rows = []
        for item in self.report.performance:
            bankruptcy = (
                r"\pending"
                if item.bankruptcy_rate is None
                else f"{item.bankruptcy_rate:.1%}".replace("%", r"\%")
            )
            values = [item.mean_score_cents, item.opponent_std_cents, item.seed_std_cents]
            performance_rows.append(
                " & ".join(
                    (
                        str(item.rank),
                        latex_text(item.label),
                        *(f"{self._money(value):,.2f}" for value in values),
                        bankruptcy,
                    )
                )
                + r" \\"
            )
        replacement_rows = []
        for item in self.report.replacements:
            values = (
                item.mean_private_difference_cents,
                item.mean_externality_per_firm_cents,
                item.mean_others_difference_cents,
                item.mean_system_difference_cents,
            )
            replacement_rows.append(
                " & ".join(
                    (
                        latex_text(item.label),
                        *(f"{self._money(value):,.2f}" for value in values),
                        rf"\multicolumn{{2}}{{c}}{{{item.positive_system_seed_count}/3}}",
                    )
                )
                + r" \\"
            )
        for filename, rows in (
            ("table2_rows.tex", performance_rows),
            ("table3_rows.tex", replacement_rows),
        ):
            (directory / filename).write_text("\n".join(rows) + "\n", encoding="utf-8")

    def render(self, destination: Path) -> None:
        """Publish six assets only after every panel and table renders successfully."""
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="ceo-paper-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            with mpl.rc_context(
                {
                    "font.family": self.language.font,
                    "font.size": MINIMUM_FONT_SIZE,
                    "pdf.fonttype": 42,
                    "axes.labelsize": MINIMUM_FONT_SIZE,
                    "xtick.labelsize": MINIMUM_FONT_SIZE,
                    "ytick.labelsize": MINIMUM_FONT_SIZE,
                    "legend.fontsize": MINIMUM_FONT_SIZE,
                    "figure.titlesize": 10,
                }
            ):
                self._performance(staging)
                self._stability(staging)
                self._replacement(staging)
                self._externalities(staging)
            self._tables(staging)
            destination.mkdir(parents=True, exist_ok=True)
            for asset in staging.iterdir():
                asset.replace(destination / asset.name)


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--analysis",
        type=Path,
        required=True,
        help="Complete audited ArenaAnalysis JSON; no model API calls are made.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[1] / "results"
    )
    parser.add_argument("--language", choices=("en", "zh"), default="en")
    parser.add_argument(
        "--bankruptcy-rates",
        type=Path,
        help="Optional audited model_id-to-rate JSON; missing rates remain pending, not inferred.",
    )
    options = parser.parse_args(arguments)
    if "unverified" in options.analysis.name.lower():
        parser.error(
            "Use the verified arena_analysis.json from the project, not an unverified artifact."
        )
    source = options.analysis.read_bytes()
    analysis = ArenaAnalysis.model_validate_json(source)
    bankruptcy_rates = None
    if options.bankruptcy_rates is not None:
        bankruptcy_rates = TypeAdapter(dict[str, float]).validate_json(
            options.bankruptcy_rates.read_bytes()
        )
    report = build_paper_report(analysis, labels=LABELS, bankruptcy_rates=bankruptcy_rates)
    PaperResultRenderer(report, language=options.language).render(options.output)
    provenance = {
        "analysis_sha256": hashlib.sha256(source).hexdigest(),
        "monetary_units": "USD thousands",
        "candidate_count": len(report.performance),
        "seeds_per_background": len(report.replacements[0].seed_points),
        "backgrounds_per_candidate": len(report.performance[0].background_means_cents),
        "bankruptcy_rates_supplied": bankruptcy_rates is not None,
        "text_width_inches": TEXT_WIDTH_INCHES,
        "panel_height_inches": PANEL_HEIGHT_INCHES,
        "minimum_font_size_points": MINIMUM_FONT_SIZE,
        "language": options.language,
    }
    (options.output / "render_provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Rendered four vector PDFs and two LaTeX tables to {options.output.resolve()}.")


if __name__ == "__main__":
    main()
