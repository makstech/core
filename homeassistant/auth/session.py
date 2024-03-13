"""Session auth module."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import secrets
from typing import TYPE_CHECKING, TypedDict

from aiohttp.web import Request
from aiohttp_session import Session, get_session, new_session
from cryptography.fernet import Fernet

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .models import RefreshToken

if TYPE_CHECKING:
    from . import AuthManager


TEMP_TIMEOUT = timedelta(minutes=5)
TEMP_TIMEOUT_SECONDS = timedelta(minutes=5).total_seconds()

SESSION_ID = "id"
STORAGE_VERSION = 1
STORAGE_KEY = "auth.session"


@dataclass
class StrictConnectionTempSessionData:
    """Session data for accessing unauthorized resources for a short period of time, when strict connection is enabled."""

    cancel_remove: CALLBACK_TYPE
    absolute_expiry: datetime = dt_util.utcnow() + TEMP_TIMEOUT


class StoreData(TypedDict):
    """Data to store."""

    unauthorized_sessions: dict[str, str]
    key: str


class SessionManager:
    """Session manager."""

    def __init__(self, hass: HomeAssistant, auth: AuthManager) -> None:
        """Initialize the strict connection manager."""
        self._auth = auth
        self._hass = hass
        self._temp_sessions: dict[str, StrictConnectionTempSessionData] = {}
        self._unauthorized_sessions: dict[str, str] = {}
        self._store = Store[StoreData](
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._key: str = None  # type: ignore[assignment]
        self._refresh_token_revoce_callbacks: dict[str, CALLBACK_TYPE] = {}

    @property
    def key(self) -> str:
        """Return the encryption key."""
        return self._key

    async def async_validate_strict_connection_session(
        self,
        request: Request,
    ) -> bool:
        """Check if a request has a valid strict connection session."""
        session = await get_session(request)
        if session.new:
            return False
        result = await self._async_validate_strict_connection_session(session)
        if result is False:
            session.invalidate()
            # todo raise for None to notify ban?
        return result

    async def _async_validate_strict_connection_session(
        self,
        session: Session,
    ) -> bool:
        if not (session_id := session.get(SESSION_ID)):
            return False

        if token_id := self._unauthorized_sessions.get(session_id):
            if self._auth.async_get_refresh_token(token_id):
                return True
            # refresh token is invalid, delete entry
            self._unauthorized_sessions.pop(session_id)
            self._async_schedule_save()

        if data := self._temp_sessions.get(session_id):
            if dt_util.utcnow() <= data.absolute_expiry:
                return True
            # session expired, delete entry
            self._temp_sessions.pop(session_id).cancel_remove()

        return False

    @callback
    def _async_register_revoke_token_callback(self, refresh_token_id: str) -> None:
        """Register a callback to revoke all sessions for a refresh token."""
        if refresh_token_id in self._refresh_token_revoce_callbacks:
            return

        @callback
        def async_invalidate_auth_sessions() -> None:
            """Invalidate all sessions for a refresh token."""
            self._unauthorized_sessions = {
                session_id: token_id
                for session_id, token_id in self._unauthorized_sessions.items()
                if token_id != refresh_token_id
            }
            self._async_schedule_save()

        self._refresh_token_revoce_callbacks[
            refresh_token_id
        ] = self._auth.async_register_revoke_token_callback(
            refresh_token_id, async_invalidate_auth_sessions
        )

    async def async_create_session(
        self,
        request: Request,
        refresh_token: RefreshToken,
    ) -> None:
        """Create new session for given refresh token."""
        if not self._auth.async_get_refresh_token(refresh_token.id):
            return

        self._unauthorized_sessions = {
            session_id: token_id
            for session_id, token_id in self._unauthorized_sessions.items()
            if token_id != refresh_token.id
        }

        self._async_register_revoke_token_callback(refresh_token.id)
        session_id = await self._async_create_new_session(request)
        self._unauthorized_sessions[session_id] = refresh_token.id
        self._async_schedule_save()

    async def async_create_temp_unauthorized_session(self, request: Request) -> None:
        """Create a temporary unauthorized session."""
        session_id = await self._async_create_new_session(
            request, max_age=int(TEMP_TIMEOUT_SECONDS)
        )

        def remove(_: datetime) -> None:
            self._temp_sessions.pop(session_id, None)

        self._temp_sessions[session_id] = StrictConnectionTempSessionData(
            async_call_later(self._hass, TEMP_TIMEOUT_SECONDS, remove)
        )

    async def _async_create_new_session(
        self,
        request: Request,
        *,
        max_age: int | None = None,
    ) -> str:
        session_id = secrets.token_hex(64)

        session = await new_session(request)
        session[SESSION_ID] = session_id
        if max_age is not None:
            session.max_age = max_age
        return session_id

    @callback
    def _async_schedule_save(self, delay: float = 1) -> None:
        """Save sessions."""
        self._store.async_delay_save(self._data_to_save, delay)

    @callback
    def _data_to_save(self) -> StoreData:
        """Return the data to store."""
        return StoreData(
            unauthorized_sessions=self._unauthorized_sessions,
            key=self._key,
        )

    async def async_setup(self) -> None:
        """Set up session manager."""
        data = await self._store.async_load()
        if data is None or not isinstance(data, dict):
            self._set_defaults()
            return

        self._key = data["key"]
        self._unauthorized_sessions = data["unauthorized_sessions"]
        for token_id in self._unauthorized_sessions.values():
            self._async_register_revoke_token_callback(token_id)

    @callback
    def _set_defaults(self) -> None:
        """Set default values."""
        self._unauthorized_sessions = {}
        self._key = _generate_key()
        self._async_schedule_save(0)


def _generate_key() -> str:
    """Generate a random key."""
    return Fernet.generate_key().decode()
