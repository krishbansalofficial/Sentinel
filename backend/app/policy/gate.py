"""Lifecycle gate on the persisted policy preset decision.

A preset is opt-in per Change Contract: without one, the gate is not in force.
With one selected, only a current ALLOW from the same snapshot a Passport v2
would sign opens the gate; any failure to build that snapshot keeps it closed.
"""

from __future__ import annotations

import logging

from backend.app.contracts.models import ChangeView
from backend.app.core.database import Database
from backend.app.core.errors import AppError

LOGGER = logging.getLogger(__name__)


def preset_allows(database: Database, change: ChangeView) -> bool:
    if change.contract.policy_preset_name is None:
        return True
    # Imported here: the Passport issuer pulls in signing and Git modules.
    from backend.app.passport.v2 import PassportV2Issuer

    try:
        snapshot = PassportV2Issuer(database).snapshot(change.id)
    except (AppError, OSError, ValueError) as exc:
        LOGGER.info("preset gate closed for %s: snapshot failed (%s)", change.id,
                    getattr(exc, "code", type(exc).__name__))
        return False
    return snapshot.policy_decision == "ALLOW" and snapshot.diff_coverage.freshness == "CURRENT"
