"""Capital allocation guard — enforces portal-managed spending limits.

Queries the portal's /api/capital/<bot_id>/limit endpoint and blocks
orders that would push open exposure above the allocation.

Design:
  - Fail-open: if the portal is unreachable and there is no cached value,
    trades are allowed (with a warning).
  - Opt-in: if the bot has no allocation entry, it trades freely.
  - 60-second cache to avoid hitting the portal on every order.
"""

import logging
import os
import time
from typing import Optional, Tuple

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_PORTAL_HOST = "host.docker.internal:8080"
_CACHE_TTL = 60  # seconds


class CapitalGuard:
    def __init__(self, bot_id: str, portal_url: Optional[str] = None,
                 portal_user: Optional[str] = None,
                 portal_pass: Optional[str] = None):
        self.bot_id = bot_id
        host = portal_url or os.environ.get("PORTAL_HOST", _DEFAULT_PORTAL_HOST)
        if not host.startswith("http"):
            host = f"http://{host}"
        self.base_url = host.rstrip("/")
        self.user = portal_user or os.environ.get("PORTAL_USER", "admin")
        self.password = portal_pass or os.environ.get("PORTAL_PASS", "changeme123")

        # Cache
        self._cached_allocation: Optional[int] = None  # cents, None = not configured
        self._cache_ts: float = 0.0
        self._ever_fetched: bool = False

        logger.info("Capital guard initialized for '%s' (portal: %s)", bot_id, self.base_url)

    def get_allocation_cents(self) -> Optional[int]:
        """Return the bot's allocation in cents, or None if not configured.

        Uses a 60-second cache.  On fetch failure with no prior cache,
        returns None (fail-open / unrestricted).
        """
        if requests is None:
            return None

        now = time.time()
        if self._ever_fetched and (now - self._cache_ts) < _CACHE_TTL:
            return self._cached_allocation

        url = f"{self.base_url}/api/capital/{self.bot_id}/limit"
        try:
            resp = requests.get(url, auth=(self.user, self.password), timeout=3)
            resp.raise_for_status()
            data = resp.json()
            self._cached_allocation = data.get("allocation_cents")  # int or None
            self._cache_ts = now
            self._ever_fetched = True
            return self._cached_allocation
        except Exception as e:
            logger.warning("Capital guard: portal unreachable (%s)", e)
            if self._ever_fetched:
                # Use stale cache
                return self._cached_allocation
            # Never fetched — fail-open
            return None

    def check_order(self, investment_cents: int,
                    current_exposure_cents: int) -> Tuple[bool, str]:
        """Check whether a new order is within the allocation.

        Args:
            investment_cents: cost of the new order in cents
            current_exposure_cents: total open exposure in cents

        Returns:
            (allowed, reason) — allowed=True means the order can proceed.
        """
        allocation = self.get_allocation_cents()

        if allocation is None:
            return (True, "no allocation configured")

        if allocation == 0:
            return (False, "allocation is $0.00 — all trades blocked")

        new_total = current_exposure_cents + investment_cents
        if new_total > allocation:
            return (False,
                    f"would exceed allocation: "
                    f"${current_exposure_cents / 100:.2f} exposure "
                    f"+ ${investment_cents / 100:.2f} order "
                    f"> ${allocation / 100:.2f} limit")

        return (True,
                f"within allocation: ${new_total / 100:.2f} / ${allocation / 100:.2f}")
