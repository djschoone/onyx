from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.connectors.connector_config import ConnectorConfig


class OsTicketConnectorConfig(ConnectorConfig):
    osticket_url: str
    batch_size: int = INDEX_BATCH_SIZE
    include_closed: bool = False
    calls_per_minute: int | None = None
