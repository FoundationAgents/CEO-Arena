"""Publish verified search receipts and bilingual appendix tables without retuning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ceo_arena.economy import MarketWorld
from ceo_arena.evaluation.rule_search import (
    FOCAL_COMPANY,
    RuleSearchPlan,
    RuleSearchReport,
    select_winner,
)
from ceo_arena.evaluation.scoring import score_world


class RuleSearchPublication:
    """Derive every published number from one complete, source-matched search."""

    def __init__(self, root: Path, report_path: Path) -> None:
        self.root = root
        self.report = RuleSearchReport.model_validate_json(report_path.read_bytes())
        self._validate()

    def _validate(self) -> None:
        report = self.report
        if report.plan != RuleSearchPlan.official(self.root):
            raise ValueError("publication requires the current frozen 24-point official search")
        if select_winner(report.plan, report.development) != report.selection:
            raise ValueError("published selection differs from development-only selection")
        if tuple(row.seed for row in report.validation) != report.plan.validation_seeds:
            raise ValueError("all three validation seeds must complete in their declared order")
        company_ids = {f"company-{index}" for index in range(1, 10)}
        initial = MarketWorld(report.plan.scenario).create_initial_state(sorted(company_ids))
        initial_scores = score_world(initial, report.plan.scenario)
        for row in (*report.development, *report.validation):
            if row.plan_hash != report.plan.plan_hash:
                raise ValueError("case belongs to another search")
            if (
                row.terminal_day != row.replay_verified_days
                or len(row.companies) != 9
                or {c.company_id for c in row.companies} != company_ids
            ):
                raise ValueError("case is missing nine-company replay evidence")
            if row.terminal_day != 500 and any(c.status == "active" for c in row.companies):
                raise ValueError("incomplete economic horizon")
            focal = next(c for c in row.companies if c.company_id == FOCAL_COMPANY)
            if focal.score_cents != row.focal_score_cents:
                raise ValueError("focal score disagrees with company receipts")
            for company in row.companies:
                starting_cash = initial.companies[company.company_id].initial_cash_cents
                starting_value = initial_scores[
                    company.company_id
                ].diagnostics.initial_enterprise_value_cents
                if (
                    company.cash_cents
                    != starting_cash
                    + company.revenue_cents
                    + company.refunds_cents
                    - company.payments_cents
                    or company.score_cents
                    != company.cash_cents + company.inventory_salvage_cents - starting_value
                ):
                    raise ValueError("published financial identity does not reconcile")
        if any(
            row.split != "validation"
            or row.candidate_id != report.selection.selected.candidate_id
            or row.config != report.selection.selected.config
            for row in report.validation
        ):
            raise ValueError("validation may evaluate only the frozen winner")

    @staticmethod
    def _money(cents: int | float) -> str:
        return f"{cents / 100:,.2f}"

    def _grid_rows(self, *, chinese: bool) -> str:
        names = (
            {"value": "价格敏感", "mainstream": "主流", "premium": "高端"}
            if chinese
            else {"value": "Value", "mainstream": "Mainstream", "premium": "Premium"}
        )
        rows: list[str] = []
        for item in self.report.selection.development_scores:
            config = item.config
            values = [
                item.candidate_id.removeprefix("candidate-"),
                f"{config.price_multiplier_bps / 10_000:.1f}",
                names[config.focus],
                "L" if config.development_daily_cents == 5_000 else "S",
                str(config.cash_floor_cents // 100_000),
                *(self._money(score) for score in item.scores_cents),
                self._money(item.mean_score_cents),
            ]
            if item.candidate_id == self.report.selection.selected.candidate_id:
                values = [rf"\textbf{{{value}}}" for value in values]
            rows.append(" & ".join(values) + r"\\")
        return "\n".join(rows)

    def _validation_rows(self, *, chinese: bool) -> str:
        rows: list[str] = []
        for item in self.report.validation:
            focal = next(c for c in item.companies if c.company_id == FOCAL_COMPANY)
            status = (
                ("存活" if focal.status == "active" else "破产") if chinese else focal.status.value
            )
            rows.append(f"{item.seed} & {self._money(item.focal_score_cents)} & {status}" + r"\\")
        mean = sum(row.focal_score_cents for row in self.report.validation) / 3
        label = "均值" if chinese else "Mean"
        rows.append(r"\midrule " + f"{label} & {self._money(mean)} & ---" + r"\\")
        return "\n".join(rows)

    def _tables(self, *, chinese: bool) -> str:
        selected = self.report.selection.selected
        caption = (
            "全部开发结果。金额为焦点公司官方 Score (美元)。L/S 表示轻量或标准投入。"
            "F 为千美元现金阈值。加粗行是按开发均值与预定平分规则选出的冠军。"
            if chinese
            else "All development results. Values are focal official Score in dollars; L/S denotes "
            "light/standard spending and F is the cash floor in thousands of dollars. "
            "Bold identifies the winner under the declared mean/tie rule."
        )
        validation_caption = (
            f"已冻结候选 {selected.candidate_id.removeprefix('candidate-')} 的独立种子验证。"
            "验证结果未用于重新选择参数。"
            if chinese
            else f"Independent-seed validation of frozen candidate "
            f"{selected.candidate_id.removeprefix('candidate-')}. "
            "Validation outcomes did not reselect parameters."
        )
        header = "ID & m & Focus & Pack & F & Seed 7 & Seed 19 & Seed 42 & Mean"
        if chinese:
            header = "ID & m & 客群 & 投入 & F & Seed 7 & Seed 19 & Seed 42 & 均值"
        validation_header = "Seed & Score / dollars & Focal status"
        if chinese:
            validation_header = "Seed & Score (美元) & 焦点公司状态"
        return "\n".join(
            (
                "% Generated from a complete RuleSearchReport; do not edit numerical rows.",
                r"\begin{table}[htbp]",
                r"\centering\small\setlength{\tabcolsep}{3pt}",
                rf"\caption{{{caption}}}",
                r"\label{tab:rule-search-results}",
                r"\begin{tabular}{@{}rclcrrrrr@{}}",
                r"\toprule " + header + r"\\\midrule",
                self._grid_rows(chinese=chinese),
                r"\bottomrule\end{tabular}",
                r"\par\bigskip",
                rf"\caption{{{validation_caption}}}",
                r"\label{tab:rule-validation-results}",
                r"\begin{tabular}{@{}rrl@{}}",
                r"\toprule " + validation_header + r"\\\midrule",
                self._validation_rows(chinese=chinese),
                r"\bottomrule\end{tabular}",
                r"\end{table}",
                "",
            )
        )

    def _write(self, relative: str, content: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def publish(self) -> None:
        report = self.report
        full_report = "docs/validation/fixed-rule-search-v3.json"
        self._write(full_report, report.model_dump_json(indent=2) + "\n")
        summary = report.model_dump(mode="json", exclude={"development"})
        self._write("paper/research/rule_search_summary.json", json.dumps(summary, indent=2) + "\n")
        for chinese, prefix in ((False, "paper"), (True, "paper/zh")):
            self._write(f"{prefix}/appendix/rule_search_results.tex", self._tables(chinese=chinese))
        print(f"Published 72 development + 3 validation receipts: {full_report}")
        print(f"Frozen winner: {report.selection.selected.candidate_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    RuleSearchPublication(Path(__file__).resolve().parents[1], args.report).publish()


if __name__ == "__main__":
    main()
