# SolarEdge ONE (v2 API) for Home Assistant

A small custom integration that reads solar production from the **SolarEdge ONE v2 monitoring API** (OAuth2) and exposes it as Home Assistant sensors, ready for the Energy dashboard.

## Why this exists

- Home Assistant's built-in `solaredge` integration uses the older v1 API (API key or username/password). SolarEdge is moving accounts to the v2 "SolarEdge ONE" API, which uses OAuth2.
- **HA's built-in OAuth2 flow (Application Credentials) doesn't work with SolarEdge.** SolarEdge doesn't return the OAuth `state` parameter on the authorization callback, so HA can't match the callback to the setup flow and setup fails. You can't fix this through settings.
- The workaround: you get the first refresh token yourself, outside HA, and paste it into this integration. From then on the integration renews it automatically.

## Sensors

| Entity | Unit | Notes |
|---|---|---|
| `sensor.solaredge_site_<SITEID>_current_power` | W | Latest 15-minute sample, not live wattage |
| `sensor.solaredge_site_<SITEID>_energy_today` | kWh | `total_increasing`, resets at midnight. Use this one in the Energy dashboard |
| `sensor.solaredge_site_<SITEID>_peak_power_today` | W | Includes a `peak_time` attribute |

## What you need first

1. **Site ID.** It's shown in the SolarEdge monitoring portal (monitoring.solaredge.com).
2. **An OAuth app** registered in the SolarEdge developer portal (<https://api-docs.solaredge.com/>). This gives you a **Client ID** and **Client Secret**. Write down the **redirect URI** you register.
3. **A refresh token**, obtained once outside HA (next section).

## Getting the first refresh token

1. Open SolarEdge's OAuth **authorize** URL in a browser with your client ID and redirect URI, then log in and approve access.
2. You're redirected to your redirect URI. Copy the `code` value from the address bar (the page itself may fail to load; that's fine).
3. Exchange the code for tokens straight away, because codes expire quickly:

   ```bash
   python3 scripts/get_refresh_token.py \
     --client-id YOUR_CLIENT_ID \
     --client-secret YOUR_CLIENT_SECRET \
     --redirect-uri YOUR_REDIRECT_URI \
     --code THE_CODE
   ```

   The script POSTs to `https://monitoringapi.solaredge.com/v2/oauth2/token` with `grant_type=authorization_code`, authenticating with HTTP Basic auth (`client_id:client_secret`), and prints the `refresh_token`.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add this repo's URL, category **Integration**.
2. Install **SolarEdge ONE (v2 API)** and restart Home Assistant.

### Manual

1. Copy `custom_components/solaredge_v2` into your HA config folder at `custom_components/solaredge_v2`.
2. Restart Home Assistant.

## Configuration

1. **Settings → Devices & Services → Add Integration → "SolarEdge ONE (v2 API)"**.
2. Enter your **Site ID**, **Client ID**, **Client Secret** and **Refresh token**.
3. The sensors appear under a device named `SolarEdge site <SITEID>`.

### Settings (after setup)

Open **Settings → Devices & Services → SolarEdge ONE (v2 API)**:

- **Configure** (polling options):
  - **Update interval**: minutes between API calls, 15–240 (default 30). The data has 15-minute resolution, so polling faster gains nothing.
  - **Only poll while the sun is up**: skips API calls between sunset and sunrise to save credits. Today's totals are kept overnight and reset at midnight. This lets you poll every 15 minutes during the day and still stay within the free tier.
- **⋮ → Reconfigure**: replace the Client ID, Client Secret and refresh token without removing the integration.
- **Re-authenticate**: if SolarEdge rejects the stored token, HA shows a repair notice. Paste a new refresh token there.

Changes to the polling options apply immediately; no restart needed.

### Energy dashboard

**Settings → Dashboards → Energy → Solar production → Add solar production:**

- Solar production energy: `sensor.solaredge_site_<SITEID>_energy_today`
- Solar production power: `sensor.solaredge_site_<SITEID>_current_power`
- Optional: add a Forecast.Solar forecast to compare against.

### Optional: alert when the API stops reporting

See [`examples/automation_api_stale.yaml`](examples/automation_api_stale.yaml). It notifies you if `energy_today` stays `unavailable` or `unknown` for 6 hours, which usually means a token or API problem, not an inverter fault.

## Things to know

- **SolarEdge issues a new refresh token every time the old one is used.** The integration saves the new token back to its config entry after each refresh. If you write your own client and skip this, it stops working after the first refresh.
- **Credit budget.** The free tier allows about 2,000 API credits per month. Each update is one call to `/v2/sites/{siteId}/power`, which returns the whole day in 15-minute steps, so current power, today's energy and today's peak all come from one request. The default of every **30 minutes** is about 1,440 calls a month. Every 15 minutes all day (about 2,900) goes over the limit and you'll start seeing 429 errors, unless you also turn on **Only poll while the sun is up**.
- **If authentication breaks** (401 errors, or the token refresh fails), HA flags the integration as needing re-authentication. Get a new refresh token using the steps above and paste it into the re-authenticate prompt.
- **Not needed:** an entry under Application Credentials, or the old built-in `solaredge` integration. Remove any left over from earlier attempts.

## Debug logging

```yaml
logger:
  logs:
    custom_components.solaredge_v2: debug
```

## License

MIT
