"""Authentication strategy — API-key only (session auth retired, þing-01 #274).

Extracted from the appwrite god-module (epic #325 S9). ``ApiKeyAuth`` never *instantiates* an SDK
``Client`` (it configures one passed in), so this module carries no test-mock surface.
"""

from abc import ABC, abstractmethod
from typing import Dict, Union

from appwrite.client import Client

from .config import AuthMethod
from .results import OperationResult


class AuthManager(ABC):
    @abstractmethod
    def setup(
        self, client: Client, credentials: Union[str, Dict[str, str]]
    ) -> OperationResult:
        pass


class ApiKeyAuth(AuthManager):
    def setup(
        self, client: Client, credentials: Union[str, Dict[str, str]]
    ) -> OperationResult:
        if not isinstance(credentials, str):
            return OperationResult(
                success=False,
                error="API key authentication requires string credentials",
                code="INVALID_CREDENTIALS",
            )

        client.set_key(credentials)
        return OperationResult(success=True)


class AuthFactory:
    @staticmethod
    def create_auth(auth_method: AuthMethod) -> AuthManager:
        if auth_method == AuthMethod.API_KEY:
            return ApiKeyAuth()
        # Session auth was RETIRED (þing-01 #274 / PLATFORM-001): the serving identity model is
        # single-mode — API key only. No serving path ever invoked session auth; it was vestigial.
        raise ValueError(f"Unsupported auth method: {auth_method} (API key only — þing-01 #274)")
