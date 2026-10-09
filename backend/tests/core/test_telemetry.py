"""Connected spans and a strict telemetry data allowlist."""
from types import SimpleNamespace

import pytest

from backend.app.core import telemetry


def test_disabled_never_loads_sdk(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_provider", None)
    assert telemetry.trace_headers() == {}
    assert telemetry.traced("test")(lambda: 42)() == 42
    assert telemetry._provider is None


@pytest.fixture
def spans(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(telemetry, "_provider", provider)
    yield exporter
    provider.shutdown()


def test_connected_trace_and_secret_allowlist(spans):
    @telemetry.traced("launch")
    def launch(change_id, run_id, command):
        return SimpleNamespace(status="PASSED", run_id=run_id, change_id=change_id)

    @telemetry.traced("eval.job")
    def job(run, task, attempt):
        launch("change-1", "agent-1", "secret-credential")
        assert "traceparent" in telemetry.trace_headers()

    job(SimpleNamespace(id="eval-1"), SimpleNamespace(id="task-1"), 1)
    child, parent = spans.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id
    assert parent.attributes["sentinel.run_id"] == "eval-1"
    assert child.attributes["sentinel.change_id"] == "change-1"
    assert "secret-credential" not in str(child.attributes)


def test_failure_does_not_export_exception_message(spans):
    @telemetry.traced("boundary.verify")
    def fail():
        raise RuntimeError("secret-credential")

    with pytest.raises(RuntimeError, match="secret-credential"):
        fail()
    span, = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert not span.events
    assert not span.status.description


def test_api_context_propagation(spans):
    import asyncio

    @telemetry.traced("eval.job")
    def parent():
        headers = telemetry.trace_headers()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})

        async def send(message):
            pass

        asyncio.run(telemetry.TraceMiddleware(app)(
            {"type": "http", "method": "POST", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]},
            None, send))

    parent()
    child, parent_span = spans.get_finished_spans()
    assert child.parent.span_id == parent_span.context.span_id
    assert child.attributes["http.response.status_code"] == 200


def test_queued_worker_spans_keep_parent_and_identifiers(spans, tmp_path):
    from backend.app.evals.queue import JobQueue
    from backend.app.evals.workers import run_pool

    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue("eval-1", ["task-a", "task-b"], 1)

    @telemetry.traced("eval.job")
    def handle(job):
        return {"ok": True}

    @telemetry.traced("eval.run")
    def parent(run_id, change_id):
        assert run_pool(queue, handle, workers=2) == 2

    parent("eval-1", "change-1")
    finished = spans.get_finished_spans()
    root = next(s for s in finished if s.name == "eval.run")
    children = [s for s in finished if s.name == "eval.job"]
    assert len(children) == 2
    for child in children:
        assert child.parent.span_id == root.context.span_id
        assert child.attributes["sentinel.run_id"] == "eval-1"
        assert child.attributes["sentinel.change_id"] == "change-1"
