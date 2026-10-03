#!/usr/bin/env python3
"""Exchange a one-time SolarEdge OAuth authorization code for tokens.

Home Assistant's built-in OAuth2 flow cannot be used with SolarEdge (the
authorization callback does not return the `state` parameter), so the first
refresh token has to be obtained by hand. Run this right after approving
access in the browser; authorization codes expire quickly.

Usage:
    python3 get_refresh_token.py \
        --client-id YOUR_CLIENT_ID \
        --client-secret YOUR_CLIENT_SECRET \
        --redirect-uri https://the.redirect/uri/registered/with/your/app \
        --code THE_CODE_FROM_THE_REDIRECT_URL

Only the Python standard library is required.
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://monitoringapi.solaredge.com/v2/oauth2/token"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--redirect-uri", required=True)
    parser.add_argument("--code", required=True)
    args = parser.parse_args()

    basic = base64.b64encode(
        f"{args.client_id}:{args.client_secret}".encode()
    ).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": args.code,
            "redirect_uri": args.redirect_uri,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.load(resp)
    except urllib.error.HTTPError as err:
        print(f"Token exchange failed ({err.code}): {err.read().decode()}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print(f"No refresh_token in response: {tokens}", file=sys.stderr)
        return 1
    print("Refresh token (paste this into Home Assistant):\n")
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
