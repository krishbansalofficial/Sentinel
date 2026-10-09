# Phase 4 tracing acceptance

Measured 2026-10-04 on Windows, Intel Core i9-14900HX, 34,070,192,128 bytes RAM, Python 3.14.3, Node 24.14.0,
Docker Desktop engine 27.3.1. This is an export/trace connectivity check, not a performance
benchmark or a real agent evaluation. OpenTelemetry SDK and HTTP exporter 1.38.0 are optional:
they provide standard parent context propagation and OTLP export without base-install cost.

Reproduce (PowerShell; use a Python environment installed with `.[test,telemetry]`):

```powershell
docker run --detach --rm --name sentinel-phase4-jaeger -p 127.0.0.1:16687:16686 -p 127.0.0.1:4319:4318 jaegertracing/jaeger:2.5.0
$env:OTEL_EXPORTER_OTLP_ENDPOINT='http://127.0.0.1:4319'
python -m backend.app.cli.main eval run --suite evals/tasks --agent mock --k 1 --task op-add --mock-skill 1 --allow-unconfined-hidden-tests --out bench/observability/mock-smoke.json
$trace = Invoke-RestMethod 'http://127.0.0.1:16687/api/traces?service=sentinel&limit=5'
$trace | ConvertTo-Json -Depth 30 | Set-Content bench/observability/jaeger-traces.json
docker stop sentinel-phase4-jaeger
```

For bash, use `export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319` and `curl` to read
the trace API. Browse `http://127.0.0.1:16687`, select service `sentinel`, and find traces.
Raw artifacts: `mock-smoke.json`, `jaeger-traces.json`. The captured trace has a
connected `eval.run` → `eval.job` → `eval.hidden_check.unconfined`, all with one trace ID.
The run passes 1/1 attempts, with Wilson interval [0.2065493144, 1.0]. Mock cost/tokens are
synthetic, and hidden tests are explicitly UNCONFINED. No Claude result is claimed.

Real runs add `agent.launch`, `agent.execute`, `boundary.verify.windows` or
`boundary.verify.linux`, and `check.run` spans. Configure the same collector in CLI and backend:
the API client propagates W3C trace context and the server restores it. Worker threads copy
context so queued jobs remain children of the eval run. Resumed processes create new traces.
Only identifier/outcome attributes are emitted, never prompts, commands, paths, credentials,
outputs or exception messages. Without an endpoint the SDK is not imported and nothing exports.
The exporter uses OTLP HTTP/protobuf; its standard endpoint, headers, TLS and timeout environment
settings apply. Remove the endpoint variables to disable it.

References: [OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/),
[Jaeger container setup](https://www.jaegertracing.io/docs/2.5/getting-started/).
