# osTicket Connector

This connector integrates osTicket support tickets into Onyx, allowing users to search and retrieve ticket information through Onyx's AI-powered search interface.

## Features

- Fetches all tickets from osTicket via the REST API
- Supports filtering by ticket status (open/closed)
- Includes full ticket thread/conversation history
- Extracts metadata including ticket number, status, department, and user information
- Rate limiting support to respect API limits

## Configuration

### Prerequisites

1. **osTicket Installation**: You need a running osTicket instance (v1.10+)
2. **osTicket API Extensions**: This connector requires extended API endpoints that are not available in the standard osTicket installation. You need to apply the API changes from:
   - **osTicket PR #6890**: [feat: Add comprehensive Ticket API with filtering, pagination, and enhanced details](https://github.com/osTicket/osTicket/pull/6890)
   
   This PR adds the required `GET /api/tickets.json` and `GET /api/tickets/{id}.json` endpoints with pagination, filtering, and detailed thread information.
   
3. **API Key**: Configure an API key in the osTicket admin panel:
   - Navigate to: Admin Panel → Manage → API Keys
   - Click "Add New API Key"
   - Note the generated API key
   - Ensure the API key is associated with the correct IP address

### Connector Settings

- **osTicket URL**: Base URL of your osTicket installation (e.g., `https://support.yourdomain.com`)
- **API Key**: The API key generated in osTicket admin panel
- **Include Closed Tickets**: Whether to index closed tickets (default: `false`)
- **Batch Size**: Number of tickets to fetch per API request (max: 100, default: 100)
- **Calls Per Minute**: Rate limiting for API calls (default: 30 per minute, ~0.5 requests/second)

## API Endpoints Used

This connector uses the following osTicket API endpoints (added by PR #6890):

- `GET /api/tickets.json` - List tickets with pagination and status filtering
  - Query parameters: `page`, `per_page`, `status`, `state`, `order`
  - Returns: `{ tickets: [...], pagination: {...} }`
  
- `GET /api/tickets/{id}.json` - Get detailed ticket information including thread
  - Returns: ticket details with `thread` array containing all messages/responses

## Thread Entry Types

The connector processes the following thread entry types from the API:

| Code | Type | Description |
|------|------|-------------|
| `M` | Customer Message | Messages sent by the ticket creator |
| `R` | Staff Response | Responses from support staff |
| `N` | Internal Note | Notes visible only to staff |

## Document Structure

Each ticket is converted into an Onyx Document with:

- **ID**: `osticket_{ticket_id}`
- **Sections**: Each message/response in the ticket thread
- **Metadata**:
  - `ticket_number`: Human-readable ticket number
  - `ticket_id`: Internal ticket ID
  - `status`: Current ticket status
  - `department`: Assigned department
  - `user_email`: Ticket creator's email
  - `user_name`: Ticket creator's name
  - `created`: Ticket creation timestamp

## Permissions

The API key must have permissions to:
- List tickets
- View ticket details
- Access ticket threads/messages

## Example Usage

```python
from onyx.connectors.osticket.connector import OsTicketConnector

connector = OsTicketConnector(
    osticket_url="https://support.example.com",
    batch_size=100,
    include_closed=False,
)

# Load credentials
connector.load_credentials({
    "osticket_api_key": "YOUR_API_KEY_HERE"
})

# Validate connection
connector.validate_connector_settings()

# Load all tickets (full indexing)
for documents in connector.load_from_state():
    print(f"Fetched {len(documents)} tickets")

# Or use poll_source for scheduled updates
# Note: osTicket API does not support time-based filtering,
# so all tickets are fetched on each poll
for documents in connector.poll_source(start=0, end=0):
    print(f"Fetched {len(documents)} tickets")
```

## Troubleshooting

### Authentication Errors

- Verify the API key is correct
- Check that the IP address is whitelisted in osTicket
- Ensure the API key hasn't expired

### Connection Errors

- Verify the osTicket URL is correct and accessible
- Check network connectivity from the Onyx server
- Ensure osTicket's API is enabled

### Rate Limiting

If you encounter rate limiting issues, configure the `calls_per_minute` parameter to a lower value.

## Limitations

- **No incremental updates**: osTicket's API does not support filtering by update time, so all tickets are fetched on each indexing run. For large installations, consider using `include_closed=False` to limit the dataset.
- **No attachment content**: Attachments are not downloaded. Attachment metadata is available in the thread entries, and URLs can be accessed via the `/api/attachments/{id}/url.json` endpoint.
- **Staff panel URLs**: Document links point to the staff control panel (`/scp/tickets.php`), which requires staff authentication.
