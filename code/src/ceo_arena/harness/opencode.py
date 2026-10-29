"""Isolated OpenCode native-LLM worker adapter."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import Field, SecretStr, TypeAdapter, field_validator, model_validator

from ceo_arena.harness.bun import resolve_bun_executable
from ceo_arena.harness.wire import (
    WorkerDecisionRequest,
    WorkerDecisionResponse,
    WorkerQueryRequest,
    WorkerQueryResponse,
    WorkerReady,
    WorkerShutdownRequest,
    WorkerValidationRequest,
    WorkerValidationResponse,
    request_id,
)
from ceo_arena.harness.worker_pool import WorkerKey, WorkerLease, WorkerPool, finish_cleanup
from ceo_arena.inference import InferenceSettings, resolve_inference
from ceo_arena.models import (
    DecisionWindowRequest,
    DecisionWindowResult,
    ProviderFailure,
    StrictModel,
)
from ceo_arena.protocol import DECISION_PROTOCOL_VERSION
from ceo_arena.runtime._redact import redact_sensitive_text
from ceo_arena.runtime.context import CompanyContext, DecisionRejectedError, QueryRejectedError

OPENCODE_LLM_COMMIT = "1be9fd55a9326d5e7b09786195e5669e311e61b4"
_TERMINATION_TIMEOUT_SECONDS = 5.0
_WORKER_REPORT_GRACE_SECONDS = 10.0
type _WorkerMessage = WorkerDecisionResponse | WorkerQueryRequest | WorkerValidationRequest
_WORKER_MESSAGE_ADAPTER = TypeAdapter(_WorkerMessage)
_WORKER_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


def _default_worker_command() -> tuple[str, ...]:
    """Use a direct Bun executable so process-tree cancellation is reliable."""

    root = Path(__file__).resolve().parents[3]
    return (resolve_bun_executable(root), "run", "src/index.ts")


class _ProcessTree:
    """Spawn and terminate a worker as one operating-system process tree."""

    @staticmethod
    def spawn_options() -> dict[str, object]:
        if sys.platform == "win32":
            return {
                "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            }
        return {"start_new_session": True}

    @staticmethod
    async def terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if sys.platform == "win32":
            taskkill = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            )
            try:
                terminator = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    await asyncio.wait_for(
                        terminator.communicate(),
                        _TERMINATION_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    if terminator.returncode is None:
                        with suppress(ProcessLookupError):
                            terminator.kill()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            terminator.wait(),
                            _TERMINATION_TIMEOUT_SECONDS,
                        )
                if terminator.returncode != 0 and process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
            except OSError:
                with suppress(ProcessLookupError):
                    process.kill()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), _TERMINATION_TIMEOUT_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.wait(), _TERMINATION_TIMEOUT_SECONDS)


def _redact_text(value: str, secret: str) -> str:
    hidden = value.replace(secret, "[REDACTED]") if secret else value
    return redact_sensitive_text(hidden) or ""


def validate_newapi_base_url(value: str) -> str:
    """Normalize an API root and forbid credential leaks over unsafe URLs."""

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url cannot contain credentials, a query, or a fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("remote NewAPI endpoints must use HTTPS")
    if parsed.path.rstrip("/").endswith("/keys"):
        raise ValueError("base_url points to a key-management page, not the model API")
    return value.rstrip("/")


class OpenCodeHarnessConfig(StrictModel):
    """Process and provider settings shared by isolated company workers."""

    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: SecretStr
    inference: InferenceSettings | None = None
    worker_command: tuple[str, ...] = Field(default_factory=_default_worker_command)
    worker_directory: Path = Path(__file__).resolve().parents[3] / "agent_worker"
    opencode_directory: Path = Path(__file__).resolve().parents[3] / "OpenCode"
    trace_directory: Path | None = None
    memory_directory: Path | None = None
    startup_timeout_seconds: float = Field(default=30.0, gt=0.0)
    shutdown_timeout_seconds: float = Field(default=5.0, gt=0.0)
    stderr_tail_lines: int = Field(default=32, ge=0, le=256)
    verify_provenance: bool = True

    @model_validator(mode="after")
    def resolve_model_inference(self) -> OpenCodeHarnessConfig:
        self.inference = resolve_inference(self.model, self.inference)
        return self

    @model_validator(mode="before")
    @classmethod
    def validate_provider_before_resolving_worker(cls, value: object) -> object:
        """Reject an invalid endpoint before probing the optional local Bun runtime."""

        if isinstance(value, dict) and isinstance(value.get("base_url"), str):
            validate_newapi_base_url(value["base_url"])
        return value

    @field_validator("worker_command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not part for part in value):
            raise ValueError("worker command must contain non-empty arguments")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return validate_newapi_base_url(value)


class _WorkerProtocolError(RuntimeError):
    """Raised when a worker violates the one-line NDJSON protocol."""


class _CompanyWorker:
    """Own one long-lived worker process for one company identity."""

    def __init__(self, company_id: str, config: OpenCodeHarnessConfig) -> None:
        self._company_id = company_id
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = deque[str](maxlen=config.stderr_tail_lines)
        self._lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the process and verify its identity/provenance handshake."""

        if self._process and self._process.returncode is None:
            return
        env = {
            key: value for key, value in os.environ.items() if key.upper() in _WORKER_ENV_ALLOWLIST
        }
        env.update(
            {
                "CEO_ARENA_API_KEY": self._config.api_key.get_secret_value(),
                "CEO_ARENA_BASE_URL": self._config.base_url,
                "CEO_ARENA_COMPANY_ID": self._company_id,
                "CEO_ARENA_MODEL": self._config.model,
                "CEO_ARENA_INFERENCE": resolve_inference(
                    self._config.model, self._config.inference
                ).model_dump_json(),
                "CEO_ARENA_OPENCODE_COMMIT": OPENCODE_LLM_COMMIT,
                "CEO_ARENA_PARENT_PID": str(os.getpid()),
            }
        )
        if self._config.trace_directory:
            env["CEO_ARENA_TRACE_DIR"] = str(self._config.trace_directory.resolve())
        if self._config.memory_directory:
            env["CEO_ARENA_MEMORY_DIR"] = str(self._config.memory_directory.resolve())
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._config.worker_command,
                cwd=self._config.worker_directory,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1 << 20,
                **_ProcessTree.spawn_options(),
            )
        )
        try:
            self._process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            await finish_cleanup(self._cancel_start(spawning))
            raise
        self._stderr_task = asyncio.create_task(self._collect_stderr())
        try:
            line = await asyncio.wait_for(
                self._readline(),
                timeout=self._config.startup_timeout_seconds,
            )
            ready = WorkerReady.model_validate_json(line)
            if (
                ready.company_id != self._company_id
                or ready.opencode_commit != OPENCODE_LLM_COMMIT
                or ready.decision_protocol_version != DECISION_PROTOCOL_VERSION
            ):
                raise _WorkerProtocolError("worker handshake identity or provenance mismatch")
        except BaseException:
            await finish_cleanup(self.kill())
            raise

    async def _cancel_start(self, spawning: asyncio.Task[asyncio.subprocess.Process]) -> None:
        """Claim a process created during cancellation before reaping its tree."""

        self._process = await spawning
        self._stderr_task = asyncio.create_task(self._collect_stderr())
        await self.kill()

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        """Send one request and accept only its correlated typed response."""

        async with self._lock:
            await self.start()
            process = self._require_process()
            message = WorkerDecisionRequest(
                request_id=request_id(request),
                request=request,
            )
            assert process.stdin is not None
            process.stdin.write(message.model_dump_json().encode() + b"\n")
            await process.stdin.drain()
            while True:
                response = _WORKER_MESSAGE_ADAPTER.validate_json(await self._readline())
                if response.request_id != message.request_id:
                    raise _WorkerProtocolError("worker response correlation mismatch")
                if isinstance(response, WorkerDecisionResponse):
                    self._validate_result(response.result, request)
                    return self._validate_trace(response.result)
                await self._answer_request(response, context)

    async def close(self) -> None:
        """Request graceful shutdown, then force termination on timeout."""

        process = self._process
        if process is None:
            return
        if process.returncode is None and process.stdin:
            with suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.write(WorkerShutdownRequest().model_dump_json().encode() + b"\n")
                await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), self._config.shutdown_timeout_seconds)
            except TimeoutError:
                await self.kill()
                return
        await self._reap(process)
        self._process = None

    async def kill(self) -> None:
        """Force-stop an unhealthy worker and reap it."""

        async with self._cleanup_lock:
            process = self._process
            if process and process.returncode is None:
                await _ProcessTree.terminate(process)
            if process:
                await self._reap(process)
            self._process = None

    async def _reap(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(process.wait(), self._config.shutdown_timeout_seconds)
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await asyncio.wait_for(process.wait(), self._config.shutdown_timeout_seconds)
        await self._finish_stderr()
        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        if process.stdout is not None:
            await process.stdout.read()

    async def _readline(self) -> str:
        process = self._require_process()
        assert process.stdout is not None
        raw = await process.stdout.readline()
        if raw:
            return raw.decode("utf-8", errors="replace")
        detail = "; ".join(self._stderr)
        suffix = f": {detail}" if detail else ""
        raise _WorkerProtocolError(f"worker exited before producing a response{suffix}")

    async def _collect_stderr(self) -> None:
        process = self._require_process()
        assert process.stderr is not None
        while raw := await process.stderr.readline():
            if self._config.stderr_tail_lines:
                self._stderr.append(self._redact(raw.decode("utf-8", errors="replace").strip()))

    async def _answer_request(
        self, request: WorkerQueryRequest | WorkerValidationRequest, context: CompanyContext
    ) -> None:
        process = self._require_process()
        assert process.stdin is not None
        response = (
            self._validate_draft(request, context)
            if isinstance(request, WorkerValidationRequest)
            else await self._query(request, context)
        )
        process.stdin.write(response.model_dump_json().encode() + b"\n")
        await process.stdin.drain()

    def _validate_draft(
        self, request: WorkerValidationRequest, context: CompanyContext
    ) -> WorkerValidationResponse:
        error = None
        try:
            context.validate_decision(request.decision)
        except DecisionRejectedError as rejected:
            error = self._redact(str(rejected))[:2_000]
        return WorkerValidationResponse(
            request_id=request.request_id, validation_id=request.validation_id, error=error
        )

    async def _query(
        self, request: WorkerQueryRequest, context: CompanyContext
    ) -> WorkerQueryResponse:
        try:
            result = await asyncio.to_thread(
                context.query,
                request.sql,
                request.parameters,
                row_limit=request.row_limit,
            )
            return WorkerQueryResponse(
                request_id=request.request_id,
                query_id=request.query_id,
                result=result,
            )
        except QueryRejectedError as rejected:
            return WorkerQueryResponse(
                request_id=request.request_id,
                query_id=request.query_id,
                error=self._redact(str(rejected))[:2_000],
            )

    async def _finish_stderr(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if not task:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise _WorkerProtocolError("worker is not running")
        return self._process

    def _redact(self, value: str) -> str:
        return _redact_text(value, self._config.api_key.get_secret_value())

    @staticmethod
    def _validate_result(
        result: DecisionWindowResult,
        request: DecisionWindowRequest,
    ) -> None:
        decision = result.decision
        if result.status == "sealed" and decision is None:
            raise _WorkerProtocolError("sealed worker result omitted its decision")
        if decision and (
            decision.company_id != request.company_id
            or decision.world_version != request.world_version
        ):
            raise _WorkerProtocolError("worker decision escaped its bound company context")

    def _validate_trace(self, result: DecisionWindowResult) -> DecisionWindowResult:
        root = self._config.trace_directory
        if root is None:
            if result.trace_path is not None or result.trace_sha256 is not None:
                raise _WorkerProtocolError("worker returned an unexpected trace artifact")
            return result
        if result.trace_path is None or result.trace_sha256 is None:
            raise _WorkerProtocolError("worker omitted its required trace receipt")
        expected_root = root.resolve()
        trace_path = Path(result.trace_path).resolve()
        if not trace_path.is_relative_to(expected_root) or not trace_path.is_file():
            raise _WorkerProtocolError("worker trace escaped its configured directory")
        with trace_path.open("rb") as trace:
            actual = hashlib.file_digest(trace, "sha256").hexdigest()
        if actual != result.trace_sha256:
            raise _WorkerProtocolError("worker trace hash mismatch")
        return result.model_copy(
            update={"trace_path": trace_path.relative_to(expected_root).as_posix()}
        )


class OpenCodeHarness:
    """Manage one isolated OpenCode native-LLM worker per company."""

    enforces_decision_budget = True

    def __init__(
        self,
        config: OpenCodeHarnessConfig,
        *,
        worker_pool: WorkerPool[_CompanyWorker] | None = None,
    ) -> None:
        self._temporary_memory: TemporaryDirectory[str] | None = None
        memory_directory = config.memory_directory
        if memory_directory is None:
            if config.trace_directory is not None:
                memory_directory = config.trace_directory.parent / "agent-memory"
            else:
                self._temporary_memory = TemporaryDirectory(prefix="ceo_arena-memory-")
                memory_directory = Path(self._temporary_memory.name)
        self._config = config.model_copy(deep=True, update={"memory_directory": memory_directory})
        self._worker_pool = worker_pool
        self._owner_id = uuid4().hex
        self._workers: dict[str, _CompanyWorker] = {}
        self._lease: ContextVar[tuple[str, WorkerLease[_CompanyWorker]] | None] = ContextVar(
            f"opencode_worker_{self._owner_id}", default=None
        )
        self._closed = False
        if config.verify_provenance:
            self._verify_provenance()

    @property
    def harness_id(self) -> str:
        """Return the pinned Harness and native LLM provenance."""

        return f"opencode-native-{OPENCODE_LLM_COMMIT[:12]}"

    @property
    def model_id(self) -> str:
        """Return the configured NewAPI model identity."""

        return self._config.model

    @property
    def inference_settings(self) -> InferenceSettings:
        """Expose the exact secret-free settings for runtime manifest validation."""

        return resolve_inference(self._config.model, self._config.inference)

    @asynccontextmanager
    async def decision_scope(self, company_id: str) -> AsyncIterator[None]:
        """Acquire a company worker before the caller starts its decision timeout."""

        if self._closed:
            raise RuntimeError("OpenCode Harness is closed")
        if self._worker_pool is None:
            yield
            return
        current = self._lease.get()
        if current is not None:
            if current[0] != company_id:
                raise RuntimeError("decision scope belongs to a different company")
            yield
            return
        key = WorkerKey(owner_id=self._owner_id, company_id=company_id)
        scope = self._worker_pool.lease(key, lambda: _CompanyWorker(company_id, self._config))
        async with scope as lease:
            token = self._lease.set((company_id, lease))
            self._workers[company_id] = lease.worker
            try:
                yield
            finally:
                self._lease.reset(token)
                if self._workers.get(company_id) is lease.worker:
                    self._workers.pop(company_id)

    async def decide(
        self,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        """Run a cancellable worker request without exposing secrets in failures."""

        if request.observation.company.company_id != request.company_id:
            return DecisionWindowResult(
                status="provider_error", error="company observation mismatch"
            )
        if request.observation.world_version != request.world_version:
            return DecisionWindowResult(status="provider_error", error="world version mismatch")
        if (
            context.company_id != request.company_id
            or context.world_version != request.world_version
        ):
            return DecisionWindowResult(status="provider_error", error="company context mismatch")
        async with self.decision_scope(request.company_id):
            worker = self._workers.get(request.company_id)
            if worker is None:
                worker = _CompanyWorker(request.company_id, self._config)
                self._workers[request.company_id] = worker
            return await self._decide(worker, request, context)

    async def _decide(
        self,
        worker: _CompanyWorker,
        request: DecisionWindowRequest,
        context: CompanyContext,
    ) -> DecisionWindowResult:
        try:
            pending = worker.decide(request, context)
            remaining = request.budget.remaining_seconds(request.prior_usage)
            result = (
                await pending
                if remaining is None
                else await asyncio.wait_for(
                    pending, timeout=remaining + _WORKER_REPORT_GRACE_SECONDS
                )
            )
            current = self._lease.get()
            if result.status not in {"sealed", "budget_exhausted"} and current is not None:
                current[1].invalidate()
            return result
        except TimeoutError:
            await finish_cleanup(self.cancel(request.company_id))
            return DecisionWindowResult(
                status="provider_error",
                error="agent worker did not report after its budget deadline and IPC grace",
                failure=ProviderFailure(
                    category="worker",
                    reason="BudgetDeadlineUnresponsive",
                    message="Worker failed to acknowledge its decision deadline",
                ),
            )
        except Exception as error:
            await finish_cleanup(self.cancel(request.company_id))
            return DecisionWindowResult(
                status="provider_error",
                error=self._safe_error(error),
                failure=ProviderFailure(
                    category="worker",
                    message="Worker execution or protocol exchange failed",
                    reason=type(error).__name__,
                    retryable=True,
                ),
            )
        except asyncio.CancelledError:
            await finish_cleanup(self.cancel(request.company_id))
            raise

    async def aclose(self) -> None:
        """Close all company workers concurrently."""

        self._closed = True
        if self._worker_pool is not None:
            try:
                await self._worker_pool.retire(self._owner_id)
            except Exception as error:
                raise RuntimeError(self._safe_error(error)) from None
            self._clean_temporary_memory()
            return
        workers = tuple(self._workers.values())
        self._workers.clear()
        tasks = tuple(asyncio.create_task(self._close_worker(worker)) for worker in workers)
        settled = asyncio.gather(*tasks, return_exceptions=True)
        try:
            results = await asyncio.shield(settled)
        except asyncio.CancelledError:
            await asyncio.shield(settled)
            raise
        secret = self._config.api_key.get_secret_value()
        errors = [
            RuntimeError(_redact_text(f"{type(result).__name__}: {result}", secret)[:2_000])
            for result in results
            if isinstance(result, Exception)
        ]
        if errors:
            raise ExceptionGroup("one or more OpenCode workers failed to close", errors)
        self._clean_temporary_memory()

    def _clean_temporary_memory(self) -> None:
        if self._temporary_memory is not None:
            self._temporary_memory.cleanup()
            self._temporary_memory = None

    async def cancel(self, company_id: str) -> None:
        """Kill and forget one worker after a cancelled decision window."""

        worker = self._workers.get(company_id)
        if worker is not None:
            current = self._lease.get()
            if current is not None and current[0] == company_id:
                current[1].invalidate()
            await worker.kill()
            if self._workers.get(company_id) is worker:
                self._workers.pop(company_id)

    def _safe_error(self, error: Exception) -> str:
        return _redact_text(str(error), self._config.api_key.get_secret_value())[:2_000]

    @staticmethod
    async def _close_worker(worker: _CompanyWorker) -> None:
        """Close one worker and force-kill it when graceful cleanup fails."""

        try:
            await worker.close()
        except asyncio.CancelledError:
            await asyncio.shield(worker.kill())
            raise
        except Exception:
            await worker.kill()
            raise

    def _verify_provenance(self) -> None:
        try:
            completed = subprocess.run(
                ("git", "-C", str(self._config.opencode_directory), "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("cannot verify pinned OpenCode checkout") from error
        if completed.stdout.strip() != OPENCODE_LLM_COMMIT:
            raise ValueError("OpenCode checkout does not match the pinned Harness commit")
        status = subprocess.run(
            (
                "git",
                "-C",
                str(self._config.opencode_directory),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            raise ValueError("pinned OpenCode checkout has tracked local modifications")
