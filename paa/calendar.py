import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from .store import digest


def http_json(url, method="GET", data=None, token=None, form=False):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    raw = None
    if data is not None:
        raw = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    request = urllib.request.Request(url, data=raw, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else {}


class Calendar:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.token_file = Path(cfg["data_dir"]) / "google-token.json"

    def client(self):
        path = Path(self.cfg["root"]) / self.cfg["calendar"]["client_file"]
        data = json.loads(path.read_text())
        if "installed" not in data:
            raise ValueError("Use a Google Desktop OAuth client")
        return data["installed"]

    def save_token(self, token):
        token["expires_at"] = time.time() + token.get("expires_in",3600)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(token))
        tmp.chmod(0o600)
        tmp.replace(self.token_file)

    def login(self):
        client = self.client()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        result = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("state", [None])[0] != state:
                    self.send_error(400)
                    return
                result.update(query)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authentication received. You may close this page.")
            def log_message(self, *args):
                pass
        with http.server.HTTPServer(("127.0.0.1",0), Handler) as server:
            server.timeout = 5
            redirect = f"http://127.0.0.1:{server.server_port}/"
            params = dict(client_id=client["client_id"], redirect_uri=redirect,
                          response_type="code", scope="https://www.googleapis.com/auth/calendar.events.owned",
                          access_type="offline", prompt="consent", state=state,
                          code_challenge=challenge, code_challenge_method="S256")
            url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
            print("Open on this Ubuntu desktop:", url)
            webbrowser.open(url)
            until = time.monotonic() + 300
            while not result and time.monotonic() < until:
                server.handle_request()
            if "code" not in result:
                raise ValueError("Google authorization did not complete")
        token = http_json("https://oauth2.googleapis.com/token", "POST",
                          dict(client_id=client["client_id"],client_secret=client.get("client_secret",""),
                               code=result["code"][0],code_verifier=verifier,redirect_uri=redirect,
                               grant_type="authorization_code"), form=True)
        self.save_token(token)

    def token(self):
        token = json.loads(self.token_file.read_text())
        if token.get("expires_at",0) < time.time() + 120:
            client = self.client()
            updated = http_json("https://oauth2.googleapis.com/token", "POST",
                                dict(client_id=client["client_id"],client_secret=client.get("client_secret",""),
                                     refresh_token=token["refresh_token"],grant_type="refresh_token"), form=True)
            token.update(updated)
            self.save_token(token)
        return token["access_token"]

    def sync(self):
        cal_id = self.cfg["calendar"].get("calendar_id", "")
        if not cal_id or cal_id == "primary" or not cal_id.endswith("@group.calendar.google.com"):
            raise ValueError("Configure a dedicated secondary Google calendar ID")
        token = self.token()
        base = "https://www.googleapis.com/calendar/v3/calendars/" + urllib.parse.quote(cal_id, safe="") + "/events"
        rows = self.store.db.execute('''SELECT e.*,d.url FROM events e JOIN docs d ON d.id=e.doc_id
          WHERE e.status='approved' AND e.source_hash=d.hash AND d.active=1''').fetchall()
        for row in rows:
            event_id = "paa" + digest(row["id"])
            key = "calendar:" + cal_id + ":" + row["id"]
            start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
            end = row["end"] or (start + timedelta(minutes=1)).isoformat()
            body = {"summary":("截止：" if row["kind"] == "deadline" else "") + row["title"],
                    "start":{"dateTime":row["start"]},"end":{"dateTime":end},"location":row["location"],
                    "description":f"来源：{row['url']}\n原文：{row['quote']}\nPAA ID: {row['id']}",
                    "extendedProperties":{"private":{"paa_id":row["id"]}},
                    "reminders":{"useDefault":False,"overrides":[{"method":"popup","minutes":2880},
                                                                      {"method":"popup","minutes":60}]}}
            endpoint = base + "/" + event_id
            try:
                remote = http_json(endpoint, token=token)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                remote = None
            if remote:
                if remote.get("extendedProperties",{}).get("private",{}).get("paa_id") != row["id"]:
                    raise ValueError("Refusing to overwrite an event not owned by PAA")
                if any(remote.get(k) != v for k,v in body.items() if k not in ("start","end")) or self.store.get(key) != row["hash"]:
                    http_json(endpoint, "PATCH", body, token)
            else:
                body["id"] = event_id
                try:
                    http_json(base, "POST", body, token)
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
                    # Deterministic ID makes recovery after an ambiguous create safe.
                    remote = http_json(endpoint, token=token)
                    if remote.get("extendedProperties",{}).get("private",{}).get("paa_id") != row["id"]:
                        raise ValueError("Calendar event ID collision")
                    http_json(endpoint, "PATCH", {k:v for k,v in body.items() if k != "id"}, token)
            self.store.set(key, row["hash"])
        return len(rows)
