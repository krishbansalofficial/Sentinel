"""GitLab provider adapter: read-only CI status for one commit, brokered calls only.

Like the GitHub adapter, it never reads a credential store or logs a token:
callers pass a bearer token resolved by
`backend.app.credentials.broker.CredentialBroker.resolve_secret` for a
``gitlab.repo.read`` grant. Commit statuses (one per job name, the latest
attempt) map onto the provider-neutral `CheckRunOutcome`, so the same
SHA-scoped verification as GitHub applies.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

from backend.app.providers.errors import (
    provider_auth_failed,
    provider_not_found,
    provider_rate_limited,
    provider_timeout,
    provider_unavailable,
)
from backend.app.providers.github import _backoff_seconds, _retry_after
from backend.app.providers.http_transport import HttpResponse, HttpTransport, TransportTimeout
from backend.app.providers.models import CheckConclusion, CheckRunOutcome

# GitLab commit status values (pipeline job states).
_STATUS_MAP: dict[str, CheckConclusion] = {
    "success": CheckConclusion.SUCCESS,
    "failed": CheckConclusion.FAILURE,
    "canceled": CheckConclusion.CANCELLED,
    "skipped": CheckConclusion.NEUTRAL,
}
PER_PAGE = 100
MAX_PAGES = 20


def map_status(status: object) -> CheckConclusion:
    """Finished states map to a conclusion; every other state is still PENDING."""
    return _STATUS_MAP.get(status if isinstance(status, str) else "", CheckConclusion.PENDING)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class GitLabProvider:
    def __init__(
        self,
        transport: HttpTransport,
        *,
        base_url: str = "https://gitlab.com/api/v4",
        max_retries: int = 3,
        retry_sleep: Callable[[float], None] = lambda _seconds: None,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.transport = transport
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._retry_sleep = retry_sleep
        self._clock = clock

    def list_check_runs_for_sha(
        self, *, token: str, repository: str, head_sha: str
    ) -> list[CheckRunOutcome]:
        """Latest status per job name for ``head_sha`` in project ``repository`` (its path).

        Pagination is bounded; a listing cut at the bound can only hide names,
        which the tracker then reports as missing required checks (PENDING).
        """
        project = quote(repository, safe="")
        results: list[CheckRunOutcome] = []
        for page in range(1, MAX_PAGES + 1):
            response = self._request(
                f"/projects/{project}/repository/commits/{quote(head_sha, safe='')}/statuses"
                f"?per_page={PER_PAGE}&page={page}", token=token)
            try:
                items = json.loads(response.body)
            except (ValueError, UnicodeDecodeError):
                raise provider_unavailable(response.status_code) from None
            if not isinstance(items, list):
                raise provider_unavailable(response.status_code)
            for item in items:
                # A record without a name or commit cannot verify anything; the
                # tracker counts its absence as a missing check (never a pass).
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or not isinstance(item.get("sha"), str)):
                    continue
                try:
                    results.append(CheckRunOutcome(
                        name=item["name"],
                        conclusion=map_status(item.get("status")),
                        head_sha=item["sha"],
                        observed_at=self._clock(),
                    ))
                except ValueError:
                    continue
            if len(items) < PER_PAGE:
                break
        return results

    def _request(self, path: str, *, token: str) -> HttpResponse:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.transport.request(
                    "GET", url, headers=headers, body=None, timeout_seconds=10.0)
            except TransportTimeout:
                if attempt > self.max_retries:
                    raise provider_timeout() from None
                self._retry_sleep(_backoff_seconds(attempt))
                continue
            if response.status_code == 200:
                return response
            if response.status_code in (401, 403):
                raise provider_auth_failed(response.status_code)
            if response.status_code == 404:
                raise provider_not_found()
            if response.status_code == 429:
                retry_after = _retry_after(response)
                if attempt > self.max_retries:
                    raise provider_rate_limited(retry_after)
                self._retry_sleep(retry_after if retry_after is not None
                                  else _backoff_seconds(attempt))
                continue
            if response.status_code >= 500:
                if attempt > self.max_retries:
                    raise provider_unavailable(response.status_code)
                self._retry_sleep(_backoff_seconds(attempt))
                continue
            raise provider_unavailable(response.status_code)
