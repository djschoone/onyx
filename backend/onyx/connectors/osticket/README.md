# osTicket Connector

This connector integrates osTicket support tickets into Onyx, allowing users to search and retrieve ticket information through Onyx's AI-powered search interface.

## Features

- Fetches all tickets from osTicket via the REST API
- Supports filtering by ticket status (open/closed)
- Includes full ticket thread/conversation history
- Extracts metadata including ticket number, status, department, and user information
- Rate limiting support to respect API limits
- Incremental updates support

## Configuration

### Prerequisites

1. **osTicket Installation**: You need a running osTicket instance (v1.10+)
2. **API Key**: Configure an API key in the osTicket admin panel:
   - Navigate to: Admin Panel → Manage → API Keys
   - Click "Add New API Key"
   - Note the generated API key
   - Ensure the API key is associated with the correct IP address

### Connector Settings

- **osTicket URL**: Base URL of your osTicket installation (e.g., `https://support.yourdomain.com`)
- **API Key**: The API key generated in osTicket admin panel
- **Include Closed Tickets**: Whether to index closed tickets (default: `false`)
- **Batch Size**: Number of tickets to fetch per API request (max: 100, default: 100)
- **Calls Per Minute**: Optional rate limiting (default: unlimited)

## API Endpoints Used

This connector uses the following osTicket API endpoints:

- `GET /api/tickets.json` - List tickets with pagination
- `GET /api/tickets/{id}.json` - Get detailed ticket information

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

# Poll for documents
for documents in connector.poll_source(start=None, end=None):
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

## Notes

- The connector currently fetches all tickets on each run (no incremental updates based on time)
- Large ticket volumes may take time to index initially
- Attachments are not currently downloaded, but attachment URLs are preserved in the ticket thread

