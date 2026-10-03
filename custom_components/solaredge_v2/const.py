"""Constants for the SolarEdge ONE (v2) integration."""

DOMAIN = "solaredge_v2"

TOKEN_URL = "https://monitoringapi.solaredge.com/v2/oauth2/token"
API_BASE = "https://monitoringapi.solaredge.com/v2"

CONF_SITE_ID = "site_id"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_EXPIRES_AT = "expires_at"

# Options (editable from the integration's Configure dialog).
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DAYLIGHT_ONLY = "daylight_only"

# Free tier allows 2000 credits/month. One /power call per cycle at 30 minutes
# is ~1440/month, which leaves headroom for restarts and token refreshes.
# /power returns the whole day at QUARTER_HOUR resolution, so a single call
# yields current power, energy today and peak - no second endpoint needed.
# Polling faster than the data resolution (15 min) gains nothing.
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 15
MAX_SCAN_INTERVAL = 240
DEFAULT_DAYLIGHT_ONLY = False

# Refresh the access token this long before it actually expires.
TOKEN_EXPIRY_MARGIN = 300
