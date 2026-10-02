"""``sentinel run``: the typical workflow in one command, without weakening any gate.

Steps: baseline (reused when one exists) -> agent launch -> workspace preview ->
optional apply -> current evidence -> optional Passport v2 issue. The flow
stops at the first gate that does not pass (agent did not succeed, preview
refused, apply not confirmed) and reports every step it ran. Apply-back keeps
its approval: it happens only for a refusal-free preview, only when asked for,
and only after confirmation (``confirm`` returns True) of the previewed paths.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from backend.app.cli.client import ApiClient, ApiError

OK = "ok"
SKIPPED = "skipped"
STOPPED = "stopped"
FAILED = "failed"

# Exit codes beyond the CLI's 0/1/2: the flow ran correctly but a gate stopped it.
EXIT_GATE_STOPPED = 4


@dataclass
class Step:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.name, "status": self.status, **self.detail}


@dataclass
class RunOptions:
    executable: str
    args: list[str]
    adapter: str = "generic"
    environment_keys: list[str] = field(default_factory=list)
    timeout_seconds: int = 900
    output_limit_bytes: int = 200_000
    apply: bool = False
    issue_passport: bool = False


@dataclass
class RunReport:
    change_id: UUID
    steps: list[Step] = field(default_factory=list)
    # The full preview (with its approval token) for the apply step; not reported.
    preview: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def outcome(self) -> str:
        if any(step.status == FAILED for step in self.steps):
            return FAILED
        if any(step.status == STOPPED for step in self.steps):
            return STOPPED
        return OK

    def as_dict(self) -> dict[str, Any]:
        return {"change_id": str(self.change_id), "outcome": self.outcome,
                "steps": [step.as_dict() for step in self.steps]}


def _workspace(change_id: UUID) -> str:
    return f"/api/v1/changes/{change_id}/workspace"


class RunFlow:
    def __init__(self, client: ApiClient, *,
                 confirm: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self._client = client
        # Without a confirmer an apply is never confirmed (non-interactive safety).
        self._confirm = confirm or (lambda _preview: False)

    def run(self, change_id: UUID, actor_id: UUID, options: RunOptions) -> RunReport:
        report = RunReport(change_id)
        for step in (self._baseline, self._launch, self._preview, self._apply,
                     self._current, self._passport):
            try:
                result = step(change_id, actor_id, options, report)
            except ApiError as error:
                result = Step(step.__name__.lstrip("_"), FAILED,
                              {"error": {"code": error.code, "message": error.message}})
            report.steps.append(result)
            if result.status in (FAILED, STOPPED):
                break
        return report

    # -- steps -------------------------------------------------------------------

    def _baseline(self, change_id, _actor, _options, _report) -> Step:
        try:
            snapshot = self._client.capture_baseline(change_id)
        except ApiError as error:
            if error.code != "BASELINE_EXISTS":
                raise
            return Step("baseline", OK, {"reused": True})
        checkpoint = snapshot.get("checkpoint", {})
        return Step("baseline", OK, {"reused": False, "head_sha": checkpoint.get("head_sha")})

    def _launch(self, change_id, actor_id, options: RunOptions, _report) -> Step:
        run = self._client.launch_agent(
            change_id, actor_id=actor_id, executable=options.executable, args=options.args,
            adapter=options.adapter, environment_keys=options.environment_keys,
            timeout_seconds=options.timeout_seconds,
            output_limit_bytes=options.output_limit_bytes)
        detail = {"run_id": run.get("id"), "agent_status": run.get("status"),
                  "exit_code": run.get("exit_code")}
        if run.get("status") != "PASSED":
            return Step("launch", STOPPED, {**detail, "reason": "the agent run did not pass"})
        return Step("launch", OK, detail)

    def _preview(self, change_id, _actor, _options, report: RunReport) -> Step:
        try:
            preview = self._client._request("POST", f"{_workspace(change_id)}/preview")
        except ApiError as error:
            if error.code != "WORKSPACE_NOT_FOUND":
                raise
            # The adapter ran in the repository itself (no AppContainer workspace).
            return Step("preview", SKIPPED,
                        {"reason": "the agent ran in the repository; there is no workspace"})
        report.preview = preview
        paths = [item.get("path") for item in preview.get("changed_paths", [])]
        detail = {"changed_paths": paths, "refusal_reason": preview.get("refusal_reason"),
                  "fast_forward_possible": preview.get("fast_forward_possible")}
        if preview.get("refusal_reason") or not preview.get("approval_token"):
            return Step("preview", STOPPED, {**detail, "reason": "apply-back is refused"})
        return Step("preview", OK, detail)

    def _apply(self, change_id, actor_id, options: RunOptions, report: RunReport) -> Step:
        if report.preview is None:
            return Step("apply", SKIPPED, {"reason": "there is no workspace to apply"})
        preview = report.preview
        token = preview.get("approval_token")
        if not options.apply:
            return Step("apply", STOPPED, {
                "reason": "apply was not requested; review the preview, then run",
                "command": f"sentinel workspace apply {change_id} {actor_id} <approval-token>",
                "approval_token": token})
        if not self._confirm(preview):
            return Step("apply", STOPPED, {"reason": "apply was not confirmed"})
        result = self._client._request(
            "POST", f"{_workspace(change_id)}/apply",
            json_body={"actor_id": str(actor_id), "approval_token": token})
        workspace = result.get("workspace", {})
        if not result.get("applied"):
            return Step("apply", STOPPED, {"reason": "apply-back was refused",
                                           "refusal_reason": workspace.get("refusal_reason")})
        return Step("apply", OK, {"applied_sha": workspace.get("applied_sha")})

    def _current(self, change_id, _actor, _options, _report) -> Step:
        snapshot = self._client.capture_current_evidence(change_id)
        checkpoint = snapshot.get("checkpoint", {})
        return Step("current_evidence", OK, {"head_sha": checkpoint.get("head_sha")})

    def _passport(self, change_id, _actor, options: RunOptions, _report) -> Step:
        if not options.issue_passport:
            return Step("passport", SKIPPED)
        issued = self._client._request("POST", f"/api/v1/changes/{change_id}/passport/v2/issue")
        payload = issued.get("payload", {})
        return Step("passport", OK, {
            "payload_digest": issued.get("payload_digest"),
            "signer_fingerprint": issued.get("signer_fingerprint"),
            "execution_boundary": payload.get("execution_boundary"),
            "policy_decision": payload.get("policy_decision")})
