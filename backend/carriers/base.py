from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from playwright.async_api import BrowserContext, Page

from ..models import Carrier, Document


class CarrierFlow(ABC):
    """Hook-based contract for a carrier-portal flow.

    A FlowRunner (in `runner.py`) drives login → MFA → fetch in a fixed
    sequence. Each carrier subclass implements the portal-specific bits.
    """

    carrier: Carrier

    def context_options(self) -> dict:
        """Optional Playwright context overrides for carrier-specific portals."""
        return {}

    @abstractmethod
    async def login(self, page: Page, username: str, password: str) -> None:
        """Navigate to the carrier's login page and submit credentials.

        After this returns, the page is either on the MFA challenge or
        already authenticated (no MFA required this session).
        """

    @abstractmethod
    async def mfa_required(self, page: Page) -> bool:
        """Return True if the page is currently asking for an MFA code."""

    @abstractmethod
    async def submit_mfa(self, page: Page, code: str) -> None:
        """Fill and submit the MFA code; navigate to the post-auth landing."""

    @abstractmethod
    async def is_authenticated(self, page: Page) -> bool:
        """Quick-path check: loaded with stored cookies, did we stay logged in?

        Should be cheap — open the carrier's dashboard URL and look for an
        authenticated-only signal (e.g. account name, sign-out link).
        """

    @abstractmethod
    async def fetch_documents(
        self,
        page: Page,
        http: httpx.AsyncClient,
        ctx: BrowserContext,
    ) -> tuple[list[Document], dict[str, bytes]]:
        """Return policy documents.

        Implementations may use `page` for navigation and `http` (with the
        carrier's auth cookies pre-loaded) for fast parallel PDF fetches.
        Returns (doc metadata, {doc_id: PDF bytes}).
        """
