from rock import env_vars

"""Configuration for LLM Service."""

# Service configuration
SERVICE_HOST = "0.0.0.0"
SERVICE_PORT = 8080

# Log file configuration
LOG_DIR = env_vars.ROCK_MODEL_SERVICE_DATA_DIR
LOG_FILE = LOG_DIR + "/LLMService.log"

# Polling configuration
POLLING_INTERVAL_SECONDS = 0.1  # seconds
REQUEST_TIMEOUT = None  # Infinite timeout as requested

# Request markers
REQUEST_START_MARKER = "LLM_REQUEST_START"
REQUEST_END_MARKER = "LLM_REQUEST_END"
RESPONSE_START_MARKER = "LLM_RESPONSE_START"
RESPONSE_END_MARKER = "LLM_RESPONSE_END"
SESSION_END_MARKER = "SESSION_END"
