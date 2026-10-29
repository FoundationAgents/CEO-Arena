"""Opaque company-scoped query capability backed by a private SQLite projection."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from types import TracebackType

from ceo_arena.models import CompanyDecision, CompanyObservation, ExpenseCategory, StrictModel

type SqlValue = int | float | str | None


class QueryResult(StrictModel):
    """Typed tabular result returned by a company-scoped query."""

    columns: tuple[str, ...]
    rows: tuple[tuple[SqlValue, ...], ...]
    truncated: bool = False


class QueryRejectedError(ValueError):
    """Raised when a query is not a single read-only SELECT statement."""


class StaleCompanyContextError(RuntimeError):
    """Raised when a Harness reuses a context after its decision window."""


class DecisionRejectedError(ValueError):
    """An economic draft is invalid but can be corrected in the same window."""


class _ContextSeal:
    """Unexported identity token required to construct a working context."""


_CONTEXT_SEAL = _ContextSeal()


class CompanyContext:
    """Non-serializable capability granting access to one immutable projection."""

    __slots__ = (
        "_active",
        "_company_id",
        "_projection",
        "_seal",
        "_timing",
        "_validator",
        "_world_version",
    )

    def __init__(
        self,
        company_id: str,
        world_version: int,
        projection: _CompanyProjection,
        seal: _ContextSeal,
        validator: Callable[[CompanyDecision], None] | None = None,
        timing: Callable[[str, float], None] | None = None,
    ) -> None:
        if seal is not _CONTEXT_SEAL:
            raise PermissionError("CompanyContext can only be issued by the coordinator")
        self._active = True
        self._company_id = company_id
        self._projection = projection
        self._seal = seal
        self._validator = validator
        self._timing = timing
        self._world_version = world_version

    @property
    def company_id(self) -> str:
        """Return the company identity bound to this capability."""

        return self._company_id

    @property
    def world_version(self) -> int:
        """Return the immutable world version represented by the projection."""

        return self._world_version

    def query(
        self,
        sql: str,
        parameters: Sequence[SqlValue] = (),
        *,
        row_limit: int = 1_000,
    ) -> QueryResult:
        """Execute one SELECT against this company's sealed data projection."""

        if not self._active:
            raise StaleCompanyContextError("company context expired after the decision window")
        started = time.perf_counter()
        try:
            return self._projection.query(sql, parameters, row_limit=row_limit)
        finally:
            if self._timing is not None:
                self._timing("sql_query", (time.perf_counter() - started) * 1000)

    def validate_decision(self, decision: CompanyDecision) -> None:
        """Check a bound draft without exposing or mutating the frozen world."""

        if not self._active:
            raise StaleCompanyContextError("company context expired after the decision window")
        if decision.company_id != self.company_id or decision.world_version != self.world_version:
            raise RuntimeError("draft escaped its bound company context")
        if self._validator is None:
            raise RuntimeError("economic validator was not attached to company context")
        started = time.perf_counter()
        try:
            self._validator(decision.model_copy(deep=True))
        except ValueError as error:
            raise DecisionRejectedError(str(error)) from error
        finally:
            if self._timing is not None:
                self._timing("economic_validation", (time.perf_counter() - started) * 1000)

    def _revoke(self) -> None:
        """Invalidate the capability and close its private database."""

        if self._active:
            self._active = False
            self._validator = None
            self._timing = None
            self._projection.close()

    def __enter__(self) -> CompanyContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._revoke()


def _issue_company_context(
    observation: CompanyObservation,
    validator: Callable[[CompanyDecision], None] | None = None,
    timing: Callable[[str, float], None] | None = None,
) -> CompanyContext:
    """Create the sole working capability form used by BenchmarkCoordinator."""

    # The synchronous builder copies scalar values into an owned SQLite database;
    # it retains no observation references, so another deep copy adds no isolation.
    projection = _CompanyProjection(observation)
    return CompanyContext(
        company_id=observation.company.company_id,
        world_version=observation.world_version,
        projection=projection,
        seal=_CONTEXT_SEAL,
        validator=validator,
        timing=timing,
    )


class _CompanyProjection:
    """Private, immutable SQLite database containing one company's view."""

    _ALLOWED_ACTIONS = frozenset(
        {
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_RECURSIVE,
            sqlite3.SQLITE_SELECT,
        }
    )
    _ALLOWED_FUNCTIONS = frozenset(
        {
            "abs",
            "avg",
            "coalesce",
            "count",
            "cume_dist",
            "dense_rank",
            "first_value",
            "ifnull",
            "lag",
            "last_value",
            "lead",
            "length",
            "likely",
            "lower",
            "max",
            "min",
            "nth_value",
            "ntile",
            "nullif",
            "percent_rank",
            "rank",
            "round",
            "row_number",
            "substr",
            "substring",
            "sum",
            "total",
            "unlikely",
            "upper",
        }
    )
    _MAX_ROWS = 10_000
    _MAX_RESULT_BYTES = 1_000_000
    _QUERY_TIMEOUT_SECONDS = 2.0

    def __init__(self, observation: CompanyObservation) -> None:
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self._MAX_RESULT_BYTES)
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 256)
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        self._connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
        self._lock = threading.RLock()
        self._closed = False
        self._build(observation)
        self._connection.execute("PRAGMA query_only = ON")
        self._connection.enable_load_extension(False)
        self._connection.set_authorizer(self._authorize)

    @staticmethod
    def _authorize(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        del argument_one, database, source
        if action == sqlite3.SQLITE_FUNCTION:
            function = (argument_two or "").casefold()
            if function not in _CompanyProjection._ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
        return (
            sqlite3.SQLITE_OK
            if action in _CompanyProjection._ALLOWED_ACTIONS
            else sqlite3.SQLITE_DENY
        )

    def query(
        self,
        sql: str,
        parameters: Sequence[SqlValue],
        *,
        row_limit: int,
    ) -> QueryResult:
        """Run a read-only query and cap materialized rows."""

        if not isinstance(sql, str) or not sql.strip():
            raise QueryRejectedError("query must be a non-empty SELECT statement")
        if row_limit <= 0 or row_limit > self._MAX_ROWS:
            raise QueryRejectedError(f"row_limit must be between 1 and {self._MAX_ROWS}")
        with self._lock:
            if self._closed:
                raise StaleCompanyContextError("company projection is closed")
            try:
                deadline = time.monotonic() + self._QUERY_TIMEOUT_SECONDS
                self._connection.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline),
                    1_000,
                )
                cursor = self._connection.execute(sql, tuple(parameters))
                columns = tuple(item[0] for item in cursor.description or ())
                rows: list[tuple[SqlValue, ...]] = []
                result_bytes = 0
                truncated = False
                while len(rows) <= row_limit:
                    row = cursor.fetchone()
                    if row is None:
                        break
                    normalized = tuple(self._normalize_value(value) for value in row)
                    result_bytes += sum(len(str(value).encode("utf-8")) for value in normalized)
                    if result_bytes > self._MAX_RESULT_BYTES:
                        truncated = True
                        break
                    rows.append(normalized)
            except sqlite3.DatabaseError as exc:
                raise QueryRejectedError(f"read-only query rejected: {exc}") from exc
            finally:
                self._connection.set_progress_handler(None, 0)
        if not columns:
            raise QueryRejectedError("only SELECT statements returning rows are allowed")
        return QueryResult(
            columns=columns,
            rows=tuple(rows[:row_limit]),
            truncated=truncated or len(rows) > row_limit,
        )

    @staticmethod
    def _normalize_value(value: object) -> SqlValue:
        """Normalize SQLite BLOB values into wire-safe deterministic hex strings."""

        if isinstance(value, bytes):
            return f"hex:{value.hex()}"
        if value is None or isinstance(value, (int, float, str)):
            return value
        return str(value)

    def close(self) -> None:
        """Close the projection exactly once."""

        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _build(self, observation: CompanyObservation) -> None:
        company = observation.company
        connection = self._connection
        connection.executescript(
            """
            CREATE TABLE scenario (
                scenario_id TEXT PRIMARY KEY,
                total_days INTEGER NOT NULL,
                current_day INTEGER NOT NULL,
                days_remaining INTEGER NOT NULL,
                days_per_decision_window INTEGER NOT NULL,
                official_score_description TEXT NOT NULL
            );
            CREATE TABLE company (
                company_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                world_version INTEGER NOT NULL,
                day INTEGER NOT NULL,
                status TEXT NOT NULL,
                cash_cents INTEGER NOT NULL,
                initial_cash_cents INTEGER NOT NULL,
                initial_enterprise_value_cents INTEGER,
                capacity_tier INTEGER NOT NULL,
                reliability_budget_cents INTEGER NOT NULL,
                support_budget_cents INTEGER NOT NULL,
                support_backlog INTEGER NOT NULL,
                cumulative_revenue_cents INTEGER NOT NULL,
                cumulative_cost_cents INTEGER NOT NULL,
                cumulative_consumer_orders INTEGER NOT NULL,
                cumulative_fulfilled_units INTEGER NOT NULL,
                cumulative_abandoned_units INTEGER NOT NULL,
                published_macro_index REAL NOT NULL
            );
            CREATE TABLE products (
                product_id TEXT PRIMARY KEY,
                price_cents INTEGER NOT NULL,
                promotion_cents INTEGER NOT NULL,
                reorder_target INTEGER NOT NULL,
                inventory INTEGER NOT NULL,
                reserved_inventory INTEGER NOT NULL,
                development_budget_cents INTEGER NOT NULL,
                knowledge_capital_cents REAL NOT NULL
            );
            CREATE TABLE product_catalog (
                product_id TEXT PRIMARY KEY,
                base_quality REAL NOT NULL,
                unit_cost_cents INTEGER NOT NULL,
                salvage_value_cents INTEGER NOT NULL,
                holding_cost_cents INTEGER NOT NULL,
                procurement_lead_days INTEGER NOT NULL
            );
            CREATE TABLE capacity_catalog (
                tier INTEGER PRIMARY KEY,
                units_per_day INTEGER NOT NULL,
                daily_cost_cents INTEGER NOT NULL,
                expansion_cost_cents INTEGER NOT NULL,
                expansion_lead_days INTEGER NOT NULL
            );
            CREATE TABLE research_catalog (
                level INTEGER PRIMARY KEY,
                cost_cents INTEGER NOT NULL,
                delay_days INTEGER NOT NULL,
                signal_std REAL NOT NULL
            );
            CREATE TABLE innovation_catalog (
                tier INTEGER PRIMARY KEY,
                cost_cents INTEGER NOT NULL,
                median_days INTEGER NOT NULL,
                expected_capital_gain_cents INTEGER NOT NULL
            );
            CREATE TABLE segments (
                segment_id TEXT PRIMARY KEY,
                goodwill REAL NOT NULL,
                awareness REAL NOT NULL,
                satisfaction REAL NOT NULL,
                loyalty REAL NOT NULL
            );
            CREATE TABLE marketing (
                segment_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                daily_budget_cents INTEGER NOT NULL,
                PRIMARY KEY (segment_id, channel)
            );
            CREATE TABLE public_prices (
                company_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                promotion_cents INTEGER NOT NULL,
                effective_price_cents INTEGER NOT NULL,
                PRIMARY KEY (company_id, product_id)
            );
            CREATE TABLE open_tenders (
                tender_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                minimum_quality REAL NOT NULL,
                reservation_price_cents INTEGER NOT NULL,
                opened_day INTEGER NOT NULL,
                deadline_day INTEGER NOT NULL,
                delivery_due_day INTEGER
            );
            CREATE TABLE tender_history (
                tender_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                opened_day INTEGER NOT NULL,
                deadline_day INTEGER NOT NULL,
                own_bid_cents INTEGER,
                status TEXT NOT NULL,
                winning_unit_price_cents INTEGER,
                delivery_due_day INTEGER
            );
            CREATE TABLE inbound_shipments (
                shipment_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                arrival_day INTEGER NOT NULL
            );
            CREATE TABLE backlog (
                order_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                product_id TEXT NOT NULL,
                segment_id TEXT,
                quantity INTEGER NOT NULL,
                unit_price_cents INTEGER NOT NULL,
                created_day INTEGER NOT NULL,
                delivery_due_day INTEGER
            );
            CREATE TABLE research_reports (
                report_id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                snapshot_day INTEGER NOT NULL,
                completed_day INTEGER NOT NULL,
                posterior_json TEXT NOT NULL
            );
            CREATE TABLE research_jobs (
                job_id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                completion_day INTEGER NOT NULL,
                status TEXT NOT NULL,
                snapshot_day INTEGER NOT NULL
            );
            CREATE TABLE beliefs (
                segment_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                mean REAL NOT NULL,
                variance REAL NOT NULL,
                updated_day INTEGER NOT NULL,
                PRIMARY KEY (segment_id, metric)
            );
            CREATE TABLE innovation_pipeline (
                contribution_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                capital_cents INTEGER NOT NULL,
                maturity_day INTEGER NOT NULL
            );
            CREATE TABLE innovation_projects (
                project_id TEXT PRIMARY KEY,
                tier INTEGER NOT NULL,
                started_day INTEGER NOT NULL,
                median_completion_day INTEGER NOT NULL,
                expected_capital_gain_cents INTEGER NOT NULL,
                status TEXT NOT NULL,
                completed_day INTEGER,
                realized_capital_gain_cents INTEGER
            );
            CREATE TABLE capacity_projects (
                project_id TEXT PRIMARY KEY,
                target_tier INTEGER NOT NULL,
                completion_day INTEGER NOT NULL,
                status TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_tier INTEGER NOT NULL,
                started_day INTEGER NOT NULL,
                paid_cents INTEGER NOT NULL,
                refund_cents INTEGER NOT NULL,
                cancelled_day INTEGER
            );
            CREATE TABLE daily_financials (
                day INTEGER PRIMARY KEY,
                revenue_cents INTEGER NOT NULL,
                costs_cents INTEGER NOT NULL,
                ending_cash_cents INTEGER NOT NULL,
                consumer_orders INTEGER NOT NULL,
                fulfilled_units INTEGER NOT NULL,
                abandoned_units INTEGER NOT NULL,
                support_backlog INTEGER NOT NULL,
                fulfillment_backlog_units INTEGER NOT NULL,
                capacity_tier INTEGER NOT NULL
            );
            CREATE TABLE daily_product_sales (
                day INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                fulfilled_units INTEGER NOT NULL,
                ending_inventory INTEGER NOT NULL,
                PRIMARY KEY (day, product_id)
            );
            CREATE TABLE daily_segment_service (
                day INTEGER NOT NULL,
                segment_id TEXT NOT NULL,
                fulfilled_units INTEGER NOT NULL,
                failures INTEGER NOT NULL,
                satisfaction REAL NOT NULL,
                PRIMARY KEY (day, segment_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO scenario VALUES (?, ?, ?, ?, ?, ?)",
            (
                observation.scenario_id,
                observation.total_days,
                observation.day,
                observation.days_remaining,
                observation.days_per_decision_window,
                observation.official_score_description,
            ),
        )
        connection.execute(
            "INSERT INTO company VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                company.company_id,
                observation.scenario_id,
                observation.world_version,
                observation.day,
                company.status.value,
                company.cash_cents,
                company.initial_cash_cents,
                company.initial_enterprise_value_cents,
                company.capacity_tier,
                company.reliability_budget_cents,
                company.support_budget_cents,
                company.support_backlog,
                company.cumulative_revenue_cents,
                company.cumulative_cost_cents,
                company.cumulative_consumer_orders,
                company.cumulative_fulfilled_units,
                company.cumulative_abandoned_units,
                observation.published_macro_index,
            ),
        )
        product_ids = sorted(
            set(company.prices_cents)
            | set(company.inventory)
            | set(company.knowledge_capital_cents)
        )
        connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    product_id,
                    company.prices_cents.get(product_id, 0),
                    company.promotions_cents.get(product_id, 0),
                    company.reorder_targets.get(product_id, 0),
                    company.inventory.get(product_id, 0),
                    company.reserved_inventory.get(product_id, 0),
                    company.development_budget_cents.get(product_id, 0),
                    company.knowledge_capital_cents.get(product_id, 0.0),
                )
                for product_id in product_ids
            ],
        )
        connection.executemany(
            "INSERT INTO product_catalog VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item.product_id,
                    item.base_quality,
                    item.unit_cost_cents,
                    item.salvage_value_cents,
                    item.holding_cost_cents,
                    item.procurement_lead_days,
                )
                for item in observation.product_catalog
            ],
        )
        connection.executemany(
            "INSERT INTO capacity_catalog VALUES (?, ?, ?, ?, ?)",
            [
                (
                    item.tier,
                    item.units_per_day,
                    item.daily_cost_cents,
                    item.expansion_cost_cents,
                    item.expansion_lead_days,
                )
                for item in observation.capacity_catalog
            ],
        )
        connection.executemany(
            "INSERT INTO research_catalog VALUES (?, ?, ?, ?)",
            [
                (item.level, item.cost_cents, item.delay_days, item.signal_std)
                for item in observation.research_catalog
            ],
        )
        connection.executemany(
            "INSERT INTO innovation_catalog VALUES (?, ?, ?, ?)",
            [
                (
                    item.tier,
                    item.cost_cents,
                    item.median_days,
                    item.expected_capital_gain_cents,
                )
                for item in observation.innovation_catalog
            ],
        )
        segment_ids = sorted(
            set(company.goodwill)
            | set(company.awareness)
            | set(company.satisfaction)
            | set(company.loyalty)
        )
        connection.executemany(
            "INSERT INTO segments VALUES (?, ?, ?, ?, ?)",
            [
                (
                    segment_id,
                    company.goodwill.get(segment_id, 0.0),
                    company.awareness.get(segment_id, 0.0),
                    company.satisfaction.get(segment_id, 0.0),
                    company.loyalty.get(segment_id, 0.0),
                )
                for segment_id in segment_ids
            ],
        )
        connection.executemany(
            "INSERT INTO marketing VALUES (?, ?, ?)",
            [
                (segment_id, channel, budget)
                for segment_id, channels in sorted(company.marketing_budget_cents.items())
                for channel, budget in sorted(channels.items())
            ],
        )
        connection.executemany(
            "INSERT INTO public_prices VALUES (?, ?, ?, ?, ?)",
            [
                (
                    public_company_id,
                    product_id,
                    price,
                    observation.public_promotions_cents[public_company_id][product_id],
                    observation.public_effective_prices_cents[public_company_id][product_id],
                )
                for public_company_id, prices in sorted(observation.public_prices_cents.items())
                for product_id, price in sorted(prices.items())
            ],
        )
        connection.executemany(
            "INSERT INTO open_tenders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    tender.tender_id,
                    tender.product_id,
                    tender.quantity,
                    tender.minimum_quality,
                    tender.reservation_price_cents,
                    tender.opened_day,
                    tender.deadline_day,
                    tender.delivery_due_day,
                )
                for tender in observation.open_tenders
                if not tender.settled
            ],
        )
        connection.executemany(
            "INSERT INTO tender_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.tender_id,
                    item.product_id,
                    item.quantity,
                    item.opened_day,
                    item.deadline_day,
                    item.own_bid_cents,
                    item.status,
                    item.winning_unit_price_cents,
                    item.delivery_due_day,
                )
                for item in observation.tender_history
            ],
        )
        connection.executemany(
            "INSERT INTO inbound_shipments VALUES (?, ?, ?, ?)",
            [
                (item.shipment_id, item.product_id, item.quantity, item.arrival_day)
                for item in company.inbound_shipments
            ],
        )
        connection.executemany(
            "INSERT INTO backlog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.order_id,
                    item.source.value,
                    item.product_id,
                    item.segment_id,
                    item.quantity,
                    item.unit_price_cents,
                    item.created_day,
                    item.delivery_due_day,
                )
                for item in company.backlog
            ],
        )
        connection.executemany(
            "INSERT INTO research_reports VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    report.report_id,
                    report.segment_id,
                    report.level,
                    report.snapshot_day,
                    report.completed_day,
                    json.dumps(
                        {
                            key: value.model_dump(mode="json")
                            for key, value in sorted(report.posterior.items())
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                for report in company.research_reports
            ],
        )
        connection.executemany(
            "INSERT INTO research_jobs VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item.job_id,
                    item.segment_id,
                    item.level,
                    item.completion_day,
                    item.status.value,
                    item.snapshot_day,
                )
                for item in company.research_jobs
            ],
        )
        connection.executemany(
            "INSERT INTO beliefs VALUES (?, ?, ?, ?, ?)",
            [
                (
                    segment_id,
                    metric,
                    belief.mean,
                    belief.variance,
                    belief.updated_day,
                )
                for segment_id, metrics in sorted(company.beliefs.items())
                for metric, belief in sorted(metrics.items())
            ],
        )
        connection.executemany(
            "INSERT INTO innovation_pipeline VALUES (?, ?, ?, ?)",
            [
                (
                    item.contribution_id,
                    item.product_id,
                    item.capital_cents,
                    item.maturity_day,
                )
                for item in company.innovation_pipeline
            ],
        )
        connection.executemany(
            "INSERT INTO innovation_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.project_id,
                    item.tier,
                    item.started_day,
                    item.median_completion_day,
                    item.expected_capital_gain_cents,
                    item.status.value,
                    item.completed_day,
                    item.realized_capital_gain_cents,
                )
                for item in observation.innovation_projects
            ],
        )
        connection.executemany(
            "INSERT INTO capacity_projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.project_id,
                    item.target_tier,
                    item.completion_day,
                    item.status.value,
                    item.kind,
                    item.source_tier,
                    item.started_day,
                    item.paid_cents,
                    item.refund_cents,
                    item.cancelled_day,
                )
                for item in company.capacity_projects
            ],
        )
        connection.executemany(
            "INSERT INTO daily_financials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.day,
                    item.revenue_cents,
                    item.costs_cents,
                    item.ending_cash_cents,
                    item.consumer_orders,
                    item.fulfilled_units,
                    item.abandoned_units,
                    item.support_backlog,
                    item.fulfillment_backlog_units,
                    item.capacity_tier,
                )
                for item in company.daily_history
            ],
        )
        connection.executemany(
            "INSERT INTO daily_product_sales VALUES (?, ?, ?, ?)",
            [
                (
                    item.day,
                    product_id,
                    item.fulfilled_by_product.get(product_id, 0),
                    inventory,
                )
                for item in company.daily_history
                for product_id, inventory in sorted(item.ending_inventory.items())
            ],
        )
        connection.executemany(
            "INSERT INTO daily_segment_service VALUES (?, ?, ?, ?, ?)",
            [
                (
                    item.day,
                    segment_id,
                    item.fulfilled_by_segment.get(segment_id, 0),
                    item.failures_by_segment.get(segment_id, 0),
                    satisfaction,
                )
                for item in company.daily_history
                for segment_id, satisfaction in sorted(item.satisfaction.items())
            ],
        )
        self._build_operating_feedback(observation)
        connection.commit()

    def _build_operating_feedback(self, observation: CompanyObservation) -> None:
        """Expose reconcilable own-company accounts and already published market signals."""

        connection = self._connection
        company = observation.company
        connection.executescript(
            """
            CREATE TABLE daily_cashflow (
                day INTEGER PRIMARY KEY,
                opening_cash_cents INTEGER NOT NULL,
                revenue_cents INTEGER NOT NULL,
                refunds_cents INTEGER NOT NULL,
                payments_cents INTEGER NOT NULL,
                cogs_cents INTEGER NOT NULL,
                gross_profit_cents INTEGER NOT NULL,
                net_cashflow_cents INTEGER NOT NULL,
                ending_cash_cents INTEGER NOT NULL
            );
            CREATE TABLE daily_expenses (
                day INTEGER NOT NULL,
                category TEXT NOT NULL,
                cents INTEGER NOT NULL,
                PRIMARY KEY (day, category)
            );
            CREATE TABLE action_receipts (
                day INTEGER NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                charged_cents INTEGER NOT NULL,
                refunded_cents INTEGER NOT NULL,
                effective_day INTEGER,
                project_id TEXT,
                target_tier INTEGER
            );
            CREATE TABLE capacity_policy (
                startup_cost_bps INTEGER NOT NULL,
                cancellation_fee_bps INTEGER NOT NULL,
                downsize_fee_per_tier_cents INTEGER NOT NULL,
                downsize_lead_days INTEGER NOT NULL
            );
            CREATE TABLE capacity_action_quotes (
                action TEXT NOT NULL,
                target_tier INTEGER,
                project_id TEXT,
                charged_cents INTEGER NOT NULL,
                refunded_cents INTEGER NOT NULL,
                effective_day INTEGER
            );
            CREATE TABLE market_signal (
                day INTEGER NOT NULL,
                demand_index REAL NOT NULL,
                quality_expectation_index REAL NOT NULL
            );
            """
        )
        opening_cash = company.initial_cash_cents
        for record in company.daily_history:
            connection.execute(
                "INSERT INTO daily_cashflow VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.day,
                    opening_cash,
                    record.revenue_cents,
                    record.refunds_cents,
                    record.costs_cents,
                    record.cogs_cents,
                    record.gross_profit_cents,
                    record.net_cashflow_cents,
                    record.ending_cash_cents,
                ),
            )
            opening_cash = record.ending_cash_cents
        connection.executemany(
            "INSERT INTO daily_expenses VALUES (?, ?, ?)",
            (
                (record.day, category.value, getattr(record.expense_breakdown, category.value))
                for record in company.daily_history
                for category in ExpenseCategory
            ),
        )
        connection.executemany(
            "INSERT INTO action_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    item.day,
                    item.action,
                    item.status,
                    item.message,
                    item.charged_cents,
                    item.refunded_cents,
                    item.effective_day,
                    item.project_id,
                    item.target_tier,
                )
                for item in company.action_receipts
            ),
        )
        policy = observation.capacity_policy
        connection.execute(
            "INSERT INTO capacity_policy VALUES (?, ?, ?, ?)",
            (
                policy.startup_cost_bps,
                policy.cancellation_fee_bps,
                policy.downsize_fee_per_tier_cents,
                policy.downsize_lead_days,
            ),
        )
        connection.executemany(
            "INSERT INTO capacity_action_quotes VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    quote.action,
                    quote.target_tier,
                    quote.project_id,
                    quote.charged_cents,
                    quote.refunded_cents,
                    quote.effective_day,
                )
                for quote in observation.capacity_action_quotes
            ),
        )
        signal = observation.published_market_signal
        connection.execute(
            "INSERT INTO market_signal VALUES (?, ?, ?)",
            (signal.day, signal.demand_index, signal.quality_expectation_index),
        )
