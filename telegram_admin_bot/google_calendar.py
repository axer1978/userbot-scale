"""Mirror booking requests into a Google Calendar.

A request appears as a tentative event the moment it is put to the provider;
the provider's YES makes it confirmed, NO removes it. Optional throughout:
with no calendar configured nothing here is called.

Authentication is a service account, because this runs unattended on a
server where nobody can click through an OAuth consent screen. Setup:

  1. Google Cloud console → create a project → enable the Google Calendar API.
  2. IAM → Service accounts → create one → Keys → add a JSON key. Save the
     file next to main.py (or anywhere; point GOOGLE_SERVICE_ACCOUNT_FILE at it).
  3. In Google Calendar, share the target calendar with the service account's
     e-mail address, permission "Make changes to events".
  4. Put that calendar's ID (Settings → Integrate calendar) in
     Settings → Bookings in the panel.

The access token is minted from a self-signed JWT, so the only dependency is
`cryptography` for the RS256 signature; it is imported lazily so the app
starts without it when no calendar is configured.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.events"
API_BASE = "https://www.googleapis.com/calendar/v3"
TOKEN_LIFETIME = 3600
# Refresh a little early so a token never expires mid-request.
TOKEN_SLACK = 120


class CalendarError(Exception):
    """Anything that stops an event being written; safe to show in the panel."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GoogleCalendar:
    def __init__(
        self,
        service_account_file: str | Path,
        calendar_id: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.calendar_id = calendar_id
        self._client = client
        self._token: Optional[str] = None
        self._token_expires = 0.0
        try:
            self._account = json.loads(Path(service_account_file).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CalendarError(
                f"Service account file not found: {service_account_file}"
            ) from None
        except (OSError, json.JSONDecodeError) as exc:
            raise CalendarError(f"Service account file unreadable: {exc}") from exc
        for key in ("client_email", "private_key", "token_uri"):
            if not self._account.get(key):
                raise CalendarError(f"Service account file is missing '{key}'.")

    # ----------------------------------------------------------- auth

    def _assertion(self) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            raise CalendarError(
                "The 'cryptography' package is needed for Google Calendar: "
                "pip install cryptography"
            ) from None

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self._account["client_email"],
            "scope": SCOPE,
            "aud": self._account["token_uri"],
            "iat": now,
            "exp": now + TOKEN_LIFETIME,
        }
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(claims, separators=(",", ":")).encode())
        )
        key = serialization.load_pem_private_key(
            self._account["private_key"].encode(), password=None
        )
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + _b64(signature)

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - TOKEN_SLACK:
            return self._token
        response = await self._http().post(
            self._account["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._assertion(),
            },
        )
        if response.status_code != 200:
            raise CalendarError(
                f"Google token request failed (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        data = response.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", TOKEN_LIFETIME))
        return self._token

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        token = await self._access_token()
        url = f"{API_BASE}/calendars/{quote(self.calendar_id, safe='')}{path}"
        response = await self._http().request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise CalendarError(
                f"Google Calendar {method} failed (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        return response.json()

    # --------------------------------------------------------- events

    async def create_event(
        self,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        tz_name: str,
        tentative: bool = True,
    ) -> str:
        """Create the event and return its id."""
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
            "status": "tentative" if tentative else "confirmed",
        }
        data = await self._request("POST", "/events", json=body)
        event_id = (data or {}).get("id")
        if not event_id:
            raise CalendarError("Google Calendar created the event but returned no id.")
        return event_id

    async def confirm_event(self, event_id: str, summary: str) -> None:
        await self._request(
            "PATCH", f"/events/{event_id}",
            json={"status": "confirmed", "summary": summary},
        )

    async def delete_event(self, event_id: str) -> None:
        try:
            await self._request("DELETE", f"/events/{event_id}")
        except CalendarError as exc:
            # Already gone is the outcome wanted; anything else is reported.
            if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
                return
            raise
