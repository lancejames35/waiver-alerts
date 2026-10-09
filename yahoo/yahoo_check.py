"""Uses the saved refresh token, gets a fresh access token, and shows Yahoo's raw response."""

import json
import os
import sys

import requests

TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

client_id = os.environ.get("YAHOO_CLIENT_ID")
client_secret = os.environ.get("YAHOO_CLIENT_SECRET")
if not client_id or not client_secret:
    sys.exit("Set YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET first.")

with open(".yahoo_tokens.json") as f:
    refresh_token = json.load(f)["refresh_token"]

t = requests.post(
    TOKEN_URL,
    auth=(client_id, client_secret),
    data={"grant_type": "refresh_token", "refresh_token": refresh_token, "redirect_uri": "https://localhost:8080"},
    timeout=30,
)
print("TOKEN REFRESH:", t.status_code)
if not t.ok:
    sys.exit(t.text)
tok = t.json()

# Yahoo may rotate the refresh token; keep the newest one.
if tok.get("refresh_token") and tok["refresh_token"] != refresh_token:
    with open(".yahoo_tokens.json", "w") as f:
        json.dump({"refresh_token": tok["refresh_token"]}, f, indent=2)
    print("Refresh token rotated and re-saved.")

headers = {"Authorization": f"Bearer {tok['access_token']}"}
for path in ["users;use_login=1/games;game_keys=nfl", "users;use_login=1/games;game_keys=nfl/leagues"]:
    r = requests.get(f"{API_BASE}/{path}", params={"format": "json"}, headers=headers, timeout=30)
    print(f"\n{path} -> {r.status_code}")
    print(r.text[:1500])
