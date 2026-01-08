"""osTicket Connector for Onyx.

This connector integrates osTicket support tickets into Onyx,
allowing users to search and retrieve ticket information.
"""

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import requests
from pydantic import BaseModel
from requests.exceptions import HTTPError

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.cross_connector_utils.miscellaneous_utils import (
    time_str_to_utc,
)
from onyx.connectors.cross_connector_utils.rate_limit_wrapper import (
    rate_limit_builder,
)
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.connectors.exceptions import CredentialExpiredError
from onyx.connectors.exceptions import InsufficientPermissionsError
from onyx.connectors.interfaces import GenerateDocumentsOutput
from onyx.connectors.interfaces import LoadConnector
from onyx.connectors.interfaces import PollConnector
from onyx.connectors.interfaces import SecondsSinceUnixEpoch
from onyx.connectors.models import BasicExpertInfo
from onyx.connectors.models import ConnectorMissingCredentialError
from onyx.connectors.models import Document
from onyx.connectors.models import TextSection
from onyx.utils.logger import setup_logger
from onyx.utils.retry_wrapper import retry_builder


logger = setup_logger()

# Constants
MAX_TICKETS_PER_PAGE = 100  # osTicket API supports up to 200
DEFAULT_CALLS_PER_MINUTE = 30  # Conservative default: ~0.5 requests per second

# Thread entry type mapping from API codes to human-readable names
_ENTRY_TYPE_MAP: dict[str, str] = {
    "M": "Customer Message",
    "R": "Staff Response",
    "N": "Internal Note",
}


class OsTicketClient:
    """Client for interacting with the osTicket API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        calls_per_minute: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        # Use default if not specified to prevent API overload
        effective_calls_per_minute = (
            calls_per_minute if calls_per_minute is not None else DEFAULT_CALLS_PER_MINUTE
        )
        self.make_request = self._request_with_rate_limit(effective_calls_per_minute)

    def _request_with_rate_limit(
        self, max_calls_per_minute: int
    ) -> Any:
        @retry_builder()
        @rate_limit_builder(max_calls=max_calls_per_minute, period=60)
        def make_request(
            endpoint: str,
            params: dict[str, Any] | None = None,
            method: str = "GET",
        ) -> dict[str, Any]:
            url = f"{self.base_url}/api/{endpoint}"

            try:
                if method == "GET":
                    response = requests.get(url, headers=self.headers, params=params)
                else:
                    response = requests.post(url, headers=self.headers, json=params)

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "60")
                    logger.warning(
                        f"Rate limited by osTicket API. Waiting {retry_after} seconds."
                    )
                    time.sleep(int(retry_after))
                    return make_request(endpoint, params, method)

                response.raise_for_status()
                return response.json()

            except HTTPError as e:
                if e.response.status_code == 401:
                    raise CredentialExpiredError(
                        "osTicket API key is invalid or expired."
                    )
                elif e.response.status_code == 403:
                    raise InsufficientPermissionsError(
                        "Insufficient permissions to access osTicket API."
                    )
                elif e.response.status_code == 404:
                    raise ConnectorValidationError(
                        f"osTicket API endpoint not found: {url}"
                    )
                else:
                    logger.error(f"HTTP error accessing osTicket API: {e}")
                    raise

        return make_request


class OsTicketPaginationInfo(BaseModel):
    """Pagination metadata from osTicket API."""

    current_page: int
    per_page: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


def _get_ticket_list(
    client: OsTicketClient,
    page: int = 1,
    per_page: int = MAX_TICKETS_PER_PAGE,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], OsTicketPaginationInfo]:
    """Fetch a page of tickets from osTicket API."""
    params: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
    }

    if status:
        params["status"] = status

    try:
        data = client.make_request("tickets.json", params=params)

        tickets = data.get("tickets", [])
        pagination_data = data.get("pagination", {})

        pagination = OsTicketPaginationInfo(
            current_page=pagination_data.get("current_page", page),
            per_page=pagination_data.get("per_page", per_page),
            total=pagination_data.get("total", 0),
            total_pages=pagination_data.get("total_pages", 1),
            has_next=pagination_data.get("has_next", False),
            has_previous=pagination_data.get("has_previous", False),
        )

        return tickets, pagination

    except Exception as e:
        logger.error(f"Error fetching ticket list: {e}")
        raise


def _get_ticket_details(
    client: OsTicketClient, ticket_id: int
) -> dict[str, Any] | None:
    """Fetch detailed information for a specific ticket."""
    try:
        data = client.make_request(f"tickets/{ticket_id}.json")
        return data
    except HTTPError as e:
        if e.response.status_code == 404:
            logger.warning(f"Ticket {ticket_id} not found")
            return None
        raise
    except Exception as e:
        logger.error(f"Error fetching ticket {ticket_id} details: {e}")
        return None


def _parse_osticket_datetime(date_str: str | None) -> datetime | None:
    """Parse osTicket datetime strings to datetime objects."""
    if not date_str:
        return None

    try:
        # osTicket typically returns datetime in format: "YYYY-MM-DD HH:MM:SS"
        return time_str_to_utc(date_str)
    except Exception as e:
        logger.warning(f"Failed to parse datetime '{date_str}': {e}")
        return None


def _get_author_name(entry: dict[str, Any]) -> str:
    """Extract author name from thread entry.

    The API returns author information in the 'author' field with structure:
    {
        "type": "staff" | "user" | "guest",
        "name": "Author Name" | {"format": "legal", "parts": {...}, "name": "Actual Name"}
    }
    Falls back to 'poster' field for backwards compatibility.
    """
    author_info = entry.get("author", {})
    if isinstance(author_info, dict):
        name_value = author_info.get("name", "Unknown")
        # osTicket API may return name as a dict with structure like:
        # {"format": "legal", "parts": {...}, "name": "Actual Name"}
        if isinstance(name_value, dict):
            # Try to get name from various possible keys
            author_name = (
                name_value.get("name")
                or name_value.get("display")
                or name_value.get("full")
            )
            # If name is in parts, try to extract it
            if author_name is None and isinstance(name_value.get("parts"), dict):
                parts = name_value.get("parts", {})
                author_name = parts.get("name") or parts.get("display")
            # Ensure we have a string, not a dict
            if isinstance(author_name, str):
                return author_name
            return "Unknown"
        elif isinstance(name_value, str):
            return name_value
        return "Unknown"

    # Fallback to poster field
    poster_value = entry.get("poster", "Unknown")
    # Ensure poster is also a string, not a dict
    if isinstance(poster_value, str):
        return poster_value
    return "Unknown"


def _get_entry_type_name(entry: dict[str, Any]) -> str:
    """Get human-readable entry type name.

    The API returns type as single letter code (M/R/N) and also
    provides display_type for frontend styling.
    """
    entry_type = entry.get("type", "")
    return _ENTRY_TYPE_MAP.get(entry_type, entry.get("display_type", entry_type))


def _convert_ticket_to_document(
    ticket_summary: dict[str, Any],
    client: OsTicketClient,
    osticket_url: str,
) -> Document | None:
    """Convert an osTicket ticket to an Onyx Document.

    Args:
        ticket_summary: Basic ticket information from list endpoint
        client: OsTicketClient instance for fetching details
        osticket_url: Base URL of osTicket installation

    Returns:
        Document object or None if conversion fails
    """
    ticket_id = ticket_summary.get("id")
    if not ticket_id:
        logger.warning("Ticket without ID encountered, skipping")
        return None

    # Fetch full ticket details
    ticket = _get_ticket_details(client, ticket_id)
    if not ticket:
        return None

    # Build document sections from ticket content
    sections: list[TextSection] = []

    # Main ticket content
    subject = ticket.get("subject", "")
    ticket_number = ticket.get("number", str(ticket_id))

    # Build the ticket URL (staff panel)
    ticket_url = f"{osticket_url}/scp/tickets.php?id={ticket_id}"

    # Add ticket initial message as first section
    initial_message = f"Ticket #{ticket_number}: {subject}\n\n"

    # Add thread entries (messages/responses)
    thread = ticket.get("thread", [])
    if thread:
        for entry in thread:
            # Get body content - API provides both 'body' (clean text) and 'body_html'
            body = entry.get("body", "")
            if not body:
                # Skip empty entries
                continue

            # Extract author name from author object
            author = _get_author_name(entry)

            # Get human-readable entry type
            entry_type = _get_entry_type_name(entry)

            created = entry.get("created", "")

            # Build message text
            message_text = f"[{entry_type}] by {author}"
            if created:
                message_text += f" on {created}"
            message_text += f":\n\n{body}"

            sections.append(
                TextSection(
                    text=message_text,
                    link=ticket_url,
                )
            )
    else:
        # If no thread, at least add the subject
        sections.append(
            TextSection(
                text=initial_message,
                link=ticket_url,
            )
        )

    # Extract metadata - status is returned as object with id, name, state
    status_info = ticket.get("status", {})
    if isinstance(status_info, dict):
        status_name = status_info.get("name", "Unknown")
    else:
        status_name = str(status_info) if status_info else "Unknown"

    # Department info
    dept_info = ticket.get("department", {})
    if isinstance(dept_info, dict):
        dept_name = dept_info.get("name", "Unknown")
    else:
        dept_name = "Unknown"

    # User info - can be None if user was deleted
    user_info = ticket.get("user")
    user_email: str | None = None
    user_name: str | None = None
    if isinstance(user_info, dict) and user_info:
        user_email = user_info.get("email")
        name_value = user_info.get("name")
        # osTicket API may return name as a dict with structure like:
        # {"format": "legal", "parts": {...}, "name": "Actual Name"}
        # or the name might be in parts or other keys
        if isinstance(name_value, dict):
            # Try to get name from various possible keys
            user_name = (
                name_value.get("name")
                or name_value.get("display")
                or name_value.get("full")
            )
            # If name is in parts, try to extract it
            if user_name is None and isinstance(name_value.get("parts"), dict):
                parts = name_value.get("parts", {})
                user_name = parts.get("name") or parts.get("display")
            # Ensure we have a string, not a dict
            if not isinstance(user_name, str):
                user_name = None
        elif isinstance(name_value, str):
            user_name = name_value
        # Ensure user_name is never a dict
        if not isinstance(user_name, (str, type(None))):
            user_name = None

    # Parse datetime
    created_at = _parse_osticket_datetime(ticket.get("created"))

    # Build metadata
    metadata: dict[str, str | list[str]] = {
        "ticket_number": ticket_number,
        "ticket_id": str(ticket_id),
        "status": status_name,
        "department": dept_name,
    }

    if user_email:
        metadata["user_email"] = user_email
    if user_name:
        metadata["user_name"] = user_name
    if ticket.get("created"):
        metadata["created"] = ticket["created"]

    # Create expert info if we have user details
    primary_owners: list[BasicExpertInfo] = []
    if user_name or user_email:
        primary_owners.append(
            BasicExpertInfo(
                display_name=user_name,
                email=user_email,
            )
        )

    # Create the document
    document = Document(
        id=f"osticket_{ticket_id}",
        sections=sections,
        source=DocumentSource.OSTICKET,
        semantic_identifier=f"Ticket #{ticket_number}: {subject}",
        doc_updated_at=created_at,
        primary_owners=primary_owners if primary_owners else None,
        metadata=metadata,
    )

    return document


class OsTicketConnector(PollConnector, LoadConnector):
    """Connector for osTicket support ticket system.

    This connector fetches tickets from an osTicket installation via its REST API
    and converts them to Onyx documents for indexing and search.

    Note: osTicket's API does not support filtering by update time, so all tickets
    are fetched on each poll. For large installations, consider using the
    include_closed=False option to limit the dataset.
    """

    def __init__(
        self,
        osticket_url: str,
        batch_size: int = INDEX_BATCH_SIZE,
        include_closed: bool = False,
        calls_per_minute: int | None = None,
    ):
        """Initialize the osTicket connector.

        Args:
            osticket_url: Base URL of the osTicket installation
            batch_size: Number of tickets to fetch per API request
            include_closed: Whether to include closed tickets
            calls_per_minute: Rate limit for API calls (default: 30 per minute)
        """
        self.osticket_url = osticket_url.rstrip("/")
        self.batch_size = min(batch_size, MAX_TICKETS_PER_PAGE)
        self.include_closed = include_closed
        # Use default if not specified to prevent API overload
        self.calls_per_minute = (
            calls_per_minute if calls_per_minute is not None else DEFAULT_CALLS_PER_MINUTE
        )
        self.client: OsTicketClient | None = None

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load and validate osTicket credentials.

        Args:
            credentials: Dictionary containing 'osticket_api_key'

        Returns:
            Dictionary with credentials or None
        """
        api_key = credentials.get("osticket_api_key")

        if not api_key:
            raise ConnectorMissingCredentialError("osticket")

        self.client = OsTicketClient(
            base_url=self.osticket_url,
            api_key=api_key,
            calls_per_minute=self.calls_per_minute,
        )

        return {"osticket_api_key": api_key}

    def validate_connector_settings(self) -> None:
        """Validate that the connector can connect to osTicket."""
        if not self.client:
            raise ConnectorMissingCredentialError("osticket")

        try:
            # Try to fetch the first page to validate credentials
            _get_ticket_list(self.client, page=1, per_page=1)
            logger.info("Successfully validated osTicket connection")
        except Exception as e:
            logger.error(f"Failed to validate osTicket connection: {e}")
            raise ConnectorValidationError(
                f"Unable to connect to osTicket: {str(e)}"
            )

    def _fetch_tickets(
        self,
    ) -> Iterator[list[dict[str, Any]]]:
        """Fetch all tickets from osTicket with pagination.

        Yields:
            Lists of ticket dictionaries
        """
        if not self.client:
            raise ConnectorMissingCredentialError("osticket")

        # Determine status filter
        status_filter = None if self.include_closed else "open"

        page = 1
        while True:
            logger.info(f"Fetching page {page} of tickets")

            tickets, pagination = _get_ticket_list(
                self.client,
                page=page,
                per_page=self.batch_size,
                status=status_filter,
            )

            if not tickets:
                logger.info("No more tickets to process")
                break

            yield tickets

            if not pagination.has_next:
                logger.info(f"Reached last page ({page})")
                break

            page += 1

    def _process_tickets(self) -> GenerateDocumentsOutput:
        """Process tickets and convert to documents.

        Yields:
            Lists of Document objects
        """
        if not self.client:
            raise ConnectorMissingCredentialError("osticket")

        total_processed = 0
        doc_batch: list[Document] = []

        for ticket_batch in self._fetch_tickets():
            for ticket in ticket_batch:
                try:
                    doc = _convert_ticket_to_document(
                        ticket, self.client, self.osticket_url
                    )
                    if doc:
                        doc_batch.append(doc)
                        total_processed += 1

                        if len(doc_batch) >= self.batch_size:
                            logger.info(f"Yielding batch of {len(doc_batch)} documents")
                            yield doc_batch
                            doc_batch = []
                except Exception as e:
                    ticket_id = ticket.get("id", "unknown")
                    logger.error(
                        f"Failed to convert ticket {ticket_id} to document: {e}"
                    )
                    continue

        # Yield remaining documents
        if doc_batch:
            logger.info(f"Yielding final batch of {len(doc_batch)} documents")
            yield doc_batch

        logger.info(f"Total tickets processed: {total_processed}")

    def load_from_state(self) -> GenerateDocumentsOutput:
        """Load all tickets from osTicket.

        Returns:
            Generator yielding lists of Documents
        """
        logger.info(f"Starting full load from osTicket at {self.osticket_url}")
        return self._process_tickets()

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        """Poll osTicket for tickets.

        Note: osTicket's API does not support filtering by update time,
        so this method fetches all tickets regardless of the time range.
        The start and end parameters are accepted for interface compatibility
        but are not used.

        Args:
            start: Start time (not used - kept for interface compatibility)
            end: End time (not used - kept for interface compatibility)

        Yields:
            Lists of Document objects
        """
        logger.info(
            f"Polling osTicket at {self.osticket_url} "
            f"(note: time filtering not supported by API)"
        )
        yield from self._process_tickets()
