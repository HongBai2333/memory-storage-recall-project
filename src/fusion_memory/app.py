import asyncio
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from fusion_memory.config import Settings
from fusion_memory.contracts import CheckpointWrite, DomainError, MemoryEvent, Receipt, Scope, SearchRequest
from fusion_memory.runtime import RootLock, Runtime


class BodyLimit:
    def __init__(self, app, limit=1_048_576):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "request_too_large",
                            "message": "Body exceeds 1 MiB",
                            "retryable": False,
                            "request_id": str(uuid4()),
                            "details": {},
                        }
                    },
                    status_code=413,
                )
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings, *, run_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        lock = RootLock(settings.data_root)
        runtime = None
        try:
            runtime = Runtime(settings)
            app.state.runtime = runtime
            if run_worker:
                runtime.thread.start()
            yield
        finally:
            if runtime:
                await run_in_threadpool(runtime.close)
            lock.close()

    app = FastAPI(
        title="Fusion Memory",
        version="0.1.0",
        lifespan=lifespan,
        description="P0/P1 text foundation. Events 1.1; receipts 1.2. Model capabilities unavailable.",
    )
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request, code, message, status, retryable=False, details=None):
        return JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "request_id": request.state.request_id,
                    "details": details or {},
                }
            },
            status_code=status,
        )

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return error_response(request, exc.code, exc.message, exc.status, exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default error includes submitted data; do not echo bodies or secrets.
        details = {"fields": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}
        return error_response(
            request, "validation_failed", "Request violates the declared schema", 422, details=details
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return error_response(request, "storage_unavailable", "Storage operation unavailable", 503, True)

    bearer = HTTPBearer(auto_error=False, scheme_name="ConnectorCredential")

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Scope:
        if credentials is None:
            raise DomainError("unauthorized", "Bearer credential required", 401)
        token = credentials.credentials
        for credential in settings.credentials:
            if secrets.compare_digest(token.encode(), credential.token.get_secret_value().encode()):
                return credential.scope
        raise DomainError("unauthorized", "Invalid credential", 401)

    def require(permission):
        def dependency(scope: Scope = Depends(authenticate)):
            if permission not in scope.permissions:
                raise DomainError("forbidden", "Required capability not granted", 403)
            return scope

        return dependency

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(request: Request):
        runtime = request.app.state.runtime
        return {
            "status": "ready",
            "memory": "available",
            "index": "available" if runtime.worker else "unavailable",
            "profile": "text-foundation",
        }

    @app.get("/v1/capabilities")
    def capabilities(request: Request, scope: Scope = Depends(require("read"))):
        runtime = request.app.state.runtime
        log, seq = runtime.memory.position(scope)
        progress = runtime.retrieval.progress(scope, log, seq)
        key = (scope.tenant_id, scope.project_id)
        return {
            "event_schema_version": "1.1",
            "receipt_schema_version": "1.2",
            "log_id": log,
            "commit_seq": seq,
            "input_types": ["text", "tool_result"],
            "capabilities": progress,
            "worker_attempts": runtime.failures.get(key, 0),
            "worker_state": "failed"
            if runtime.failures.get(key, 0) >= 5
            else "running"
            if runtime.worker and run_worker
            else "stopped",
        }

    @app.post("/v1/events", response_model=Receipt)
    def ingest(event: MemoryEvent, request: Request, scope: Scope = Depends(require("write"))):
        return request.app.state.runtime.memory.ingest(scope, event, request.state.request_id)

    @app.get("/v1/sources/{event_id}")
    def read_source(event_id: str, request: Request, scope: Scope = Depends(require("read"))):
        result = request.app.state.runtime.memory.read(scope, event_id)
        if result is None:
            raise DomainError("not_found", "Source not found or no longer active", 404)
        return result

    @app.get("/v1/processing-status/{event_id}")
    def processing(event_id: str, request: Request, scope: Scope = Depends(require("read"))):
        runtime = request.app.state.runtime
        log, seq = runtime.memory.receipt_position(scope, event_id)
        return {
            "event_id": event_id,
            "log_id": log,
            "commit_seq": seq,
            "raw_committed": True,
            "capabilities": runtime.retrieval.progress(scope, log, seq),
        }

    @app.post("/v1/memory/search")
    @app.post("/v1/context/assemble")
    async def search(body: SearchRequest, request: Request, scope: Scope = Depends(require("read"))):
        runtime = request.app.state.runtime
        if body.project_id != scope.project_id:
            raise DomainError("scope_mismatch", "Project does not match credential", 403)
        if body.min_commit_seq and not body.log_id:
            raise DomainError("log_id_required", "min_commit_seq requires its log_id", 422)
        if not body.min_commit_seq:
            log, seq = await run_in_threadpool(runtime.memory.position, scope)
            body = body.model_copy(update={"log_id": body.log_id or log, "min_commit_seq": seq})
        deadline = time.monotonic() + body.wait_timeout_ms / 1000
        while True:
            result = await run_in_threadpool(runtime.retrieval.search, scope, body)
            if result["status"] != "pending" or time.monotonic() >= deadline:
                return JSONResponse(result, status_code=202 if result["status"] == "pending" else 200)
            if await request.is_disconnected():
                raise DomainError("cancelled", "Caller disconnected; committed work remains durable", 499)
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    @app.post("/v1/checkpoints")
    def save_checkpoint(
        body: CheckpointWrite, request: Request, scope: Scope = Depends(require("checkpoint"))
    ):
        return request.app.state.runtime.checkpoints.save(scope, body)

    @app.get("/v1/checkpoints/{checkpoint_id}")
    def read_checkpoint(
        checkpoint_id: str,
        workflow_version: str,
        request: Request,
        scope: Scope = Depends(require("checkpoint")),
    ):
        return request.app.state.runtime.checkpoints.read(scope, checkpoint_id, workflow_version)

    return app
