"""osTicket Connector for Onyx.

This connector integrates osTicket support tickets into Onyx,
allowing users to search and retrieve ticket information.
"""

import time
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from typing import Any

import requests
from pydantic import BaseModel
from requests.exceptions import HTTPError
from typing_extensions import override

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
from onyx.connectors.interfaces import CheckpointedConnector
from onyx.connectors.interfaces import CheckpointOutput
from onyx.connectors.interfaces import GenerateSlimDocumentOutput
from onyx.connectors.interfaces import SecondsSinceUnixEpoch
from onyx.connectors.models import BasicExpertInfo
from onyx.connectors.models import ConnectorCheckpoint
from onyx.connectors.models import Document
from onyx.connectors.models import DocumentFailure
from onyx.connectors.models import SlimDocument
from onyx.connectors.models import TextSection
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.utils.logger import setup_logger
from onyx.utils.retry_wrapper import retry_builder


logger = setup_logger()

# Constants
MAX_TICKETS_PER_PAGE = 100  # osTicket API supports up to 200
_SLIM_BATCH_SIZE = 500


class OsTicketCredentialsNotSetUpError(PermissionError):
    def __init__(self) -> None:
        super().__init__(
            "osTicket credentials are not set up. Was load_credentials called?"
        )


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
        self.make_request = self._request_with_rate_limit(calls_per_minute)

    def _request_with_rate_limit(
        self, max_calls_per_minute: int | None = None
    ) -> Any:
        @retry_builder()
        @(
            rate_limit_builder(max_calls=max_calls_per_minute, period=60)
            if max_calls_per_minute
            else lambda x: x
        )
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
                    response = requests.post(
                        url, headers=self.headers, json=params
                    )

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
    params = {
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


class OsTicketConnector(CheckpointedConnector):
    """Connector for osTicket support ticket system."""

    def __init__(
        self,
        osticket_url: str,
        batch_size: int = MAX_TICKETS_PER_PAGE,
        include_closed: bool = False,
        calls_per_minute: int | None = None,
    ):
        """Initialize the osTicket connector.

        Args:
            osticket_url: Base URL of the osTicket installation
            batch_size: Number of tickets to fetch per API request
            include_closed: Whether to include closed tickets
            calls_per_minute: Rate limit for API calls
        """
        self.osticket_url = osticket_url.rstrip("/")
        self.batch_size = min(batch_size, MAX_TICKETS_PER_PAGE)
        self.include_closed = include_closed
        self.calls_per_minute = calls_per_minute
        self.client: OsTicketClient | None = None

    @override
    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load and validate osTicket credentials.

        Args:
            credentials: Dictionary containing 'osticket_api_key'

        Returns:
            Dictionary with credentials or None
        """
        api_key = credentials.get("osticket_api_key")
        
        if not api_key:
            raise ConnectorValidationError("osTicket API key is required")

        self.client = OsTicketClient(
            base_url=self.osticket_url,
            api_key=api_key,
            calls_per_minute=self.calls_per_minute,
        )

        return {"osticket_api_key": api_key}

    @override
    def validate_connector_settings(self) -> None:
        """Validate that the connector can connect to osTicket."""
        if not self.client:
            raise OsTicketCredentialsNotSetUpError()

        try:
            # Try to fetch the first page to validate credentials
            _get_ticket_list(self.client, page=1, per_page=1)
            logger.info("Successfully validated osTicket connection")
        except Exception as e:
            logger.error(f"Failed to validate osTicket connection: {e}")
            raise ConnectorValidationError(
                f"Unable to connect to osTicket: {str(e)}"
            )

    def _convert_ticket_to_document(
        self, ticket_summary: dict[str, Any]
    ) -> Document | None:
        """Convert an osTicket ticket to an Onyx Document.

        Args:
            ticket_summary: Basic ticket information

        Returns:
            Document object or None if conversion fails
        """
        if not self.client:
            raise OsTicketCredentialsNotSetUpError()

        ticket_id = ticket_summary.get("id")
        if not ticket_id:
            logger.warning("Ticket without ID encountered, skipping")
            return None

        # Fetch full ticket details
        ticket = _get_ticket_details(self.client, ticket_id)
        if not ticket:
            return None

        # Build document sections from ticket content
        sections: list[TextSection] = []
        
        # Main ticket content
        subject = ticket.get("subject", "")
        ticket_number = ticket.get("number", str(ticket_id))
        
        # Build the ticket URL
        ticket_url = f"{self.osticket_url}/scp/tickets.php?id={ticket_id}"
        
        # Add ticket initial message as first section
        initial_message = f"Ticket #{ticket_number}: {subject}\n\n"
        
        # Add thread entries (messages/responses)
        thread = ticket.get("thread", [])
        if thread:
            for entry in thread:
                body = entry.get("body", "")
                author = entry.get("poster", "Unknown")
                entry_type = entry.get("type", "")
                created = entry.get("created", "")
                
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

        # Extract metadata
        status_info = ticket.get("status", {})
        status_name = status_info.get("name", "Unknown") if isinstance(status_info, dict) else str(status_info)
        
        dept_info = ticket.get("department", {})
        dept_name = dept_info.get("name", "Unknown") if isinstance(dept_info, dict) else "Unknown"
        
        user_info = ticket.get("user", {})
        user_email = user_info.get("email") if isinstance(user_info, dict) else None
        user_name = user_info.get("name") if isinstance(user_info, dict) else None
        
        # Parse datetime
        created_at = _parse_osticket_datetime(ticket.get("created"))
        
        # Build metadata
        metadata: dict[str, Any] = {
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

    @override
    def poll_source(
        self, start: SecondsSinceUnixEpoch | None, end: SecondsSinceUnixEpoch | None
    ) -> Iterator[list[Document]]:
        """Poll osTicket for tickets and convert to Onyx documents.

        Args:
            start: Start time for incremental updates (not used currently)
            end: End time for incremental updates (not used currently)

        Yields:
            Lists of Document objects
        """
        if not self.client:
            raise OsTicketCredentialsNotSetUpError()

        logger.info(f"Starting osTicket poll from {self.osticket_url}")
        
        # Determine status filter
        status_filter = None if self.include_closed else "open"
        
        page = 1
        total_tickets_processed = 0
        
        while True:
            logger.info(f"Fetching page {page} of tickets")
            
            try:
                tickets, pagination = _get_ticket_list(
                    self.client,
                    page=page,
                    per_page=self.batch_size,
                    status=status_filter,
                )
                
                if not tickets:
                    logger.info("No more tickets to process")
                    break

                # Convert tickets to documents
                documents: list[Document] = []
                for ticket in tickets:
                    try:
                        doc = self._convert_ticket_to_document(ticket)
                        if doc:
                            documents.append(doc)
                    except Exception as e:
                        ticket_id = ticket.get("id", "unknown")
                        logger.error(
                            f"Failed to convert ticket {ticket_id} to document: {e}"
                        )
                        continue

                if documents:
                    total_tickets_processed += len(documents)
                    logger.info(
                        f"Yielding {len(documents)} documents from page {page}"
                    )
                    yield documents

                # Check if there are more pages
                if not pagination.has_next:
                    logger.info(
                        f"Reached last page. Total tickets processed: {total_tickets_processed}"
                    )
                    break

                page += 1

            except Exception as e:
                logger.error(f"Error fetching tickets on page {page}: {e}")
                raise

    @override
    def retrieve_all_slim_documents(
        self,
        start: SecondsSinceUnixEpoch | None = None,
        end: SecondsSinceUnixEpoch | None = None,
    ) -> GenerateSlimDocumentOutput:
        """Retrieve slim document representations for permission syncing.

        Args:
            start: Start time for filtering (not used)
            end: End time for filtering (not used)

        Yields:
            Lists of SlimDocument objects
        """
        if not self.client:
            raise OsTicketCredentialsNotSetUpError()

        logger.info("Starting slim document retrieval from osTicket")
        
        status_filter = None if self.include_closed else "open"
        page = 1
        
        while True:
            try:
                tickets, pagination = _get_ticket_list(
                    self.client,
                    page=page,
                    per_page=_SLIM_BATCH_SIZE,
                    status=status_filter,
                )
                
                if not tickets:
                    break

                slim_docs: list[SlimDocument] = []
                for ticket in tickets:
                    ticket_id = ticket.get("id")
                    if not ticket_id:
                        continue
                    
                    # Parse the created datetime
                    created_at = _parse_osticket_datetime(ticket.get("created"))
                    
                    slim_doc = SlimDocument(
                        id=f"osticket_{ticket_id}",
                        perm_sync_data={
                            "ticket_id": str(ticket_id),
                            "number": ticket.get("number", ""),
                        },
                        updated_at=created_at,
                    )
                    slim_docs.append(slim_doc)

                if slim_docs:
                    yield slim_docs

                if not pagination.has_next:
                    break

                page += 1

            except Exception as e:
                logger.error(f"Error retrieving slim documents on page {page}: {e}")
                raise

    @override
    def _get_next_checkpoint(
        self, checkpoint: ConnectorCheckpoint, iteration_info: CheckpointOutput
    ) -> ConnectorCheckpoint:
        """Update checkpoint after processing.

        Args:
            checkpoint: Current checkpoint
            iteration_info: Information about the current iteration

        Returns:
            Updated checkpoint
        """
        # For osTicket, we process all tickets in each run
        # so we just mark as complete
        checkpoint.has_more = iteration_info.has_more
        return checkpoint

