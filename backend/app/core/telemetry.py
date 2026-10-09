"""Opt-in tracing. Only identifiers and outcomes leave the runtime, never commands or secrets."""
from __future__ import annotations

import atexit
import inspect
import os
import threading
from contextvars import ContextVar
from functools import wraps

_lock = threading.Lock()
_provider = None
_correlation = ContextVar("sentinel_trace_correlation", default=None)


def tracer():
    global _provider
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or
            os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        return None
    with _lock:
        if _provider is None:
            try:
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            except ModuleNotFoundError:
                raise RuntimeError(
                    "OTLP tracing requires the telemetry extra: pip install -e '.[telemetry]'"
                ) from None

            provider = TracerProvider(resource=Resource.create({"service.name": "sentinel"}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            _provider = provider
            atexit.register(provider.shutdown)
        return _provider.get_tracer("sentinel.runtime")


def traced(name: str):
    """Preserve parent context and attach a small allowlist of correlation attributes."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            current = tracer()
            if current is None:
                return function(*args, **kwargs)
            values = signature.bind(*args, **kwargs).arguments
            attributes = dict(_correlation.get() or {})
            for key in ("change_id", "run_id", "attempt"):
                if values.get(key) is not None:
                    attributes[f"sentinel.{key}"] = str(values[key])
            run = values.get("run")
            if run is not None:
                attributes["sentinel.run_id"] = str(run.id)
            box = getattr(values.get("self"), "_record", None)
            if box is not None:
                for key in ("change_id", "id"):
                    value = getattr(box, key, None)
                    if value is not None:
                        attributes["sentinel.change_id" if key == "change_id" else "sentinel.check_run_id"] = str(value)
            task = values.get("task")
            if task is not None:
                attributes["sentinel.task_id"] = task.id
            # Exception messages may contain commands, paths or credentials.
            token = _correlation.set(attributes)
            try:
                return _invoke(current, name, attributes, function, args, kwargs)
            finally:
                _correlation.reset(token)
        return wrapped
    return decorate


def _invoke(current, name, attributes, function, args, kwargs):
    with current.start_as_current_span(name, attributes=attributes,
                                       record_exception=False,
                                       set_status_on_exception=False) as span:
        try:
            result = function(*args, **kwargs)
        except BaseException:
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR))
            raise
        outcome = getattr(result, "status", None)
        outcome = getattr(outcome, "value", outcome)
        if name == "eval.run":
            identifier = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
            if identifier is not None:
                span.set_attribute("sentinel.run_id", str(identifier))
        if name == "agent.launch" and getattr(result, "id", None) is not None:
            span.set_attribute("sentinel.run_id", str(result.id))
        for key in ("change_id", "run_id"):
            value = getattr(result, key, None)
            if value is not None:
                span.set_attribute(f"sentinel.{key}", str(value))
        if outcome is not None:
            span.set_attribute("sentinel.outcome", str(outcome))
            if str(outcome) in ("ERROR", "FAILED"):
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR))
        return result


def trace_headers() -> dict[str, str]:
    if tracer() is None:
        return {}
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    headers: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(headers)
    return headers


class TraceMiddleware:
    """Propagate eval CLI traces across the API and into FastAPI's worker context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        current = tracer() if scope["type"] == "http" else None
        if current is None:
            return await self.app(scope, receive, send)
        from opentelemetry.trace import SpanKind
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        headers = {key.decode("latin1"): value.decode("latin1") for key, value in scope.get("headers", [])}
        context = TraceContextTextMapPropagator().extract(headers)
        # No URL, query string, request body, authorization or exception text.
        with current.start_as_current_span("api.request", context=context, kind=SpanKind.SERVER,
                                           attributes={"http.request.method": scope["method"]},
                                           record_exception=False, set_status_on_exception=False) as span:
            async def traced_send(message):
                if message["type"] == "http.response.start":
                    span.set_attribute("http.response.status_code", message["status"])
                await send(message)
            return await self.app(scope, receive, traced_send)
