"""
One-time Yahoo OAuth2 setup for the waiver alert worker.

1. Opens Yahoo's consent page.
2. You approve, browser lands on https://localhost:8080/?code=... (page won't load, that's fine).
3. Paste that full URL back here.
4. Exchanges the code for tokens, saves them to .yahoo_tokens.json,
   and lists your NFL leagues so we can pick the two to watch.

Env vars required: YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET
"""

import json
import os
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
REDIRECT_URI = "https://localhost:8080"
TOKEN_FILE = ".yahoo_tokens.json"


def find_leagues(node, out):
    """Yahoo's JSON nests leagues under numbered keys; walk it and grab anything with a league_key."""
    if isinstance(node, dict):
        if "league_key" in node and "name" in node:
            out.append(node)
        for v in node.values():
            find_leagues(v, out)
    elif isinstance(node, list):
        for v in node:
            find_leagues(v, out)
    return out


def main():
    client_id = os.environ.get("YAHOO_CLIENT_ID")
    client_secret = os.environ.get("YAHOO_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET first.")

    url = f"{AUTH_URL}?{urlencode({'client_id': client_id, 'redirect_uri': REDIRECT_URI, 'response_type': 'code', 'scope': 'fspt-r'})}"
    print(f"\nOpening Yahoo consent page. If it doesn't open, go here:\n{url}\n")
    webbrowser.open(url)

    pasted = input("Paste the full localhost URL from the address bar (or just the code): ").strip()
    code = parse_qs(urlparse(pasted).query)["code"][0] if pasted.startswith("http") else pasted

    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        timeout=30,
    )
    if not resp.ok:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    tokens = resp.json()

    with open(TOKEN_FILE, "w") as f:
        json.dump({"refresh_token": tokens["refresh_token"]}, f, indent=2)
    print(f"\nRefresh token saved to {TOKEN_FILE}. Don't commit it.")

    r = requests.get(
        f"{API_BASE}/users;use_login=1/games;game_keys=nfl/leagues",
        params={"format": "json"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=30,
    )
    r.raise_for_status()
    leagues = find_leagues(r.json(), [])

    if not leagues:
        print("\nAuth worked but no NFL leagues came back. Check you're on the right Yahoo account.")
        return

    print("\nYour Yahoo NFL leagues:")
    for lg in leagues:
        print(f"  {lg['league_key']:<20} {lg['name']}  ({lg.get('num_teams', '?')} teams, {lg.get('scoring_type', '')})")


if __name__ == "__main__":
    main()
