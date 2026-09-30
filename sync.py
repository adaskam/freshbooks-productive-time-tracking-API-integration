#!/usr/bin/env python3
"""
FreshBooks -> Productive.io time entry sync.

Commands:
  python sync.py auth                                   One-time FreshBooks OAuth setup
  python sync.py discover --since 2026-09-01            Show FreshBooks IDs to put in mapping.json
  python sync.py discover --productive                  Show Productive people and service IDs
  python sync.py sync --since 2026-09-01 --until 2026-09-30 [--dry-run] [--delete-missing]

Config comes from environment variables (or a .env file); see .env.example.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("fb2productive")

FB_API = "https://api.freshbooks.com"
FB_AUTH_URL = "https://auth.freshbooks.com/oauth/authorize"
FB_TOKEN_URL = f"{FB_API}/auth/oauth/token"
PRODUCTIVE_API = "https://api.productive.io/api/v2"


# --------------------------------------------------------------------------- helpers

def env(name, default=None, required=True):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_json(path, default):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def save_json(path, data):
    """Atomic write so a crash mid-save can't corrupt tokens or state."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


class APIError(Exception):
    def __init__(self, resp):
        self.status = resp.status_code
        super().__init__(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text[:500]}")


def retry_after_seconds(value, fallback):
    """Retry-After may be delay-seconds or an HTTP-date; fall back to backoff if unparseable."""
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def request_with_retry(session, method, url, max_retries=5, **kwargs):
    """
    Retries on rate limiting (429) and server errors with backoff.
    A 5xx on POST is not retried: the server may have created the resource
    before failing, so retrying could create a duplicate.
    """
    resp = None
    for attempt in range(max_retries):
        resp = session.request(method, url, timeout=30, **kwargs)
        if resp.status_code >= 500 and method.upper() == "POST":
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = retry_after_seconds(resp.headers.get("Retry-After"), 2 ** attempt)
            log.warning("%s %s returned %s, retrying in %.0fs", method, url, resp.status_code, wait)
            time.sleep(wait)
            continue
        return resp
    return resp


# --------------------------------------------------------------------------- FreshBooks

class FreshBooksClient:
    def __init__(self):
        self.client_id = env("FRESHBOOKS_CLIENT_ID")
        self.client_secret = env("FRESHBOOKS_CLIENT_SECRET")
        self.redirect_uri = env("FRESHBOOKS_REDIRECT_URI")
        self.token_file = env("FRESHBOOKS_TOKEN_FILE", "freshbooks_tokens.json", required=False)
        self.tokens = load_json(self.token_file, {})
        self.session = requests.Session()

    # ---- OAuth
    def authorize_url(self):
        query = urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
        })
        return f"{FB_AUTH_URL}?{query}"

    def exchange_code(self, code):
        self._token_request({"grant_type": "authorization_code", "code": code})

    def _token_request(self, extra):
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            **extra,
        }
        resp = self.session.post(FB_TOKEN_URL, json=payload, timeout=30)
        if not resp.ok:
            raise APIError(resp)
        data = resp.json()
        # FreshBooks refresh tokens are single-use: the new one MUST be saved,
        # or the next run will fail and you'll have to re-run `auth`.
        self.tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + int(data.get("expires_in", 43200)) - 300,
        }
        save_json(self.token_file, self.tokens)

    def _access_token(self):
        if not self.tokens.get("refresh_token"):
            sys.exit("No FreshBooks tokens found. Run: python sync.py auth")
        if time.time() >= self.tokens.get("expires_at", 0):
            log.info("Refreshing FreshBooks access token")
            self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],
            })
        return self.tokens["access_token"]

    # ---- API
    def get(self, path, params=None):
        def call():
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            return request_with_retry(self.session, "GET", FB_API + path, headers=headers, params=params)

        resp = call()
        if resp.status_code == 401:  # token expired early or was revoked; force one refresh
            self.tokens["expires_at"] = 0
            resp = call()
        if not resp.ok:
            raise APIError(resp)
        return resp.json()

    def business_id(self):
        explicit = os.environ.get("FRESHBOOKS_BUSINESS_ID")
        if explicit:
            return explicit
        me = self.get("/auth/api/v1/users/me")["response"]
        memberships = me.get("business_memberships", [])
        if len(memberships) == 1:
            return memberships[0]["business"]["id"]
        options = ", ".join(f'{m["business"]["id"]} ({m["business"].get("name")})' for m in memberships)
        sys.exit(f"Set FRESHBOOKS_BUSINESS_ID to one of: {options}")

    def time_entries(self, business_id, start_utc, end_utc):
        page = 1
        while True:
            data = self.get(
                f"/timetracking/business/{business_id}/time_entries",
                params={
                    "started_from": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_to": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "page": page,
                    "per_page": 100,
                },
            )
            yield from data.get("time_entries", [])
            if page >= int(data.get("meta", {}).get("pages", 1)):
                break
            page += 1


# --------------------------------------------------------------------------- Productive

class ProductiveClient:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            "X-Auth-Token": env("PRODUCTIVE_API_TOKEN"),
            "X-Organization-Id": str(env("PRODUCTIVE_ORG_ID")),
            "Content-Type": "application/vnd.api+json",
        })

    def _call(self, method, path, body=None, params=None, allow_404=False):
        resp = request_with_retry(
            self.session, method, PRODUCTIVE_API + path,
            data=json.dumps(body) if body is not None else None,
            params=params,
        )
        if allow_404 and resp.status_code == 404:
            return None
        if not resp.ok:
            raise APIError(resp)
        return resp.json() if resp.content else {}

    @staticmethod
    def _body(attrs, person_id, service_id, entry_id=None):
        data = {
            "type": "time_entries",
            "attributes": attrs,
            "relationships": {
                "person": {"data": {"type": "people", "id": str(person_id)}},
                "service": {"data": {"type": "services", "id": str(service_id)}},
            },
        }
        if entry_id:
            data["id"] = str(entry_id)
        return {"data": data}

    def create_time_entry(self, attrs, person_id, service_id):
        if self.dry_run:
            log.info("[dry-run] CREATE %s", attrs)
            return None
        return self._call("POST", "/time_entries", self._body(attrs, person_id, service_id))["data"]["id"]

    def update_time_entry(self, entry_id, attrs, person_id, service_id):
        """Returns the entry id, or None if it no longer exists in Productive."""
        if self.dry_run:
            log.info("[dry-run] UPDATE %s %s", entry_id, attrs)
            return entry_id
        result = self._call("PATCH", f"/time_entries/{entry_id}",
                            self._body(attrs, person_id, service_id, entry_id), allow_404=True)
        return result["data"]["id"] if result else None

    def delete_time_entry(self, entry_id):
        if self.dry_run:
            log.info("[dry-run] DELETE %s", entry_id)
            return
        self._call("DELETE", f"/time_entries/{entry_id}", allow_404=True)

    def list_all(self, path, params=None):
        page, included = 1, {}
        rows = []
        while True:
            data = self._call("GET", path, params={**(params or {}), "page[number]": page, "page[size]": 200})
            rows.extend(data.get("data", []))
            for inc in data.get("included", []):
                included[(inc["type"], inc["id"])] = inc
            if page >= int(data.get("meta", {}).get("total_pages", 1)):
                return rows, included
            page += 1


# --------------------------------------------------------------------------- mapping

class Mapping:
    """
    Resolves a FreshBooks entry to a Productive person + service.
    Service rules match on any of fb_client_id / fb_project_id / fb_service_id;
    the most specific matching rule wins.
    """

    def __init__(self, path):
        cfg = load_json(path, None)
        if cfg is None:
            sys.exit(f"Mapping file not found: {path} (copy mapping.example.json)")
        self.people = {str(k): str(v) for k, v in cfg.get("people", {}).items()}
        self.default_person = cfg.get("default_person_id")
        self.rules = cfg.get("services", [])
        self.default_service = cfg.get("default_service_id")

    def person_for(self, entry):
        return self.people.get(str(entry.get("identity_id"))) or self.default_person

    def service_for(self, entry):
        best, best_score = None, -1
        for rule in self.rules:
            score, matches = 0, True
            for key in ("fb_client_id", "fb_project_id", "fb_service_id"):
                if key in rule:
                    if str(rule[key]) != str(entry.get(key[3:])):
                        matches = False
                        break
                    score += 1
            if matches and score > best_score:
                best, best_score = rule["productive_service_id"], score
        return best or self.default_service


# --------------------------------------------------------------------------- sync logic

def utc_range(since, until, tz):
    """Local-date window [since, until] -> UTC datetimes."""
    start = datetime.combine(since, dtime.min, tz).astimezone(timezone.utc)
    end = datetime.combine(until + timedelta(days=1), dtime.min, tz).astimezone(timezone.utc)
    return start, end


def local_date(started_at, tz):
    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if dt.tzinfo is None:  # FreshBooks timestamps are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def build_note(entry, fb_id):
    note = (entry.get("note") or "").strip()
    if os.environ.get("TAG_NOTES", "true").lower() == "true":
        note = f"{note} [FreshBooks #{fb_id}]".strip()
    return note


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def should_skip(entry):
    if (entry.get("timer") or {}).get("is_running"):
        return "timer still running"
    if entry.get("is_logged") is False:
        return "not logged"
    return None


def cmd_sync(args):
    tz = ZoneInfo(env("SYNC_TIMEZONE", "UTC", required=False))
    fb = FreshBooksClient()
    prod = ProductiveClient(dry_run=args.dry_run)
    mapping = Mapping(env("MAPPING_FILE", "mapping.json", required=False))
    state_file = env("STATE_FILE", "sync_state.json", required=False)
    state = load_json(state_file, {})

    business_id = fb.business_id()
    start_utc, end_utc = utc_range(args.since, args.until, tz)
    log.info("Syncing %s..%s (%s) from FreshBooks business %s", args.since, args.until, tz, business_id)

    stats = Counter()
    seen = set()

    for entry in fb.time_entries(business_id, start_utc, end_utc):
        fb_id = str(entry["id"])
        entry_date = local_date(entry["started_at"], tz)
        if not (args.since <= entry_date <= args.until):
            continue
        seen.add(fb_id)

        reason = should_skip(entry)
        if reason:
            log.debug("Skip %s: %s", fb_id, reason)
            stats["skipped"] += 1
            continue

        person, service = mapping.person_for(entry), mapping.service_for(entry)
        if not person or not service:
            log.warning("Unmapped entry %s (identity=%s client=%s project=%s service=%s)",
                        fb_id, entry.get("identity_id"), entry.get("client_id"),
                        entry.get("project_id"), entry.get("service_id"))
            stats["unmapped"] += 1
            continue

        minutes = round(int(entry.get("duration") or 0) / 60)
        if minutes <= 0:
            stats["skipped"] += 1
            continue

        attrs = {"date": entry_date.isoformat(), "time": minutes, "note": build_note(entry, fb_id)}
        fp = fingerprint({"attrs": attrs, "person": person, "service": service})
        existing = state.get(fb_id)

        if existing and existing["fingerprint"] == fp:
            stats["unchanged"] += 1
            continue

        try:
            productive_id = None
            if existing:
                productive_id = prod.update_time_entry(existing["productive_id"], attrs, person, service)
                stats["updated" if productive_id else "recreated"] += 1
            if not productive_id:  # new, or deleted in Productive since last sync
                productive_id = prod.create_time_entry(attrs, person, service)
                if not existing:
                    stats["created"] += 1
        except APIError as err:
            log.error("Failed to sync FreshBooks entry %s: %s", fb_id, err)
            stats["errors"] += 1
            continue

        if not args.dry_run:
            state[fb_id] = {"productive_id": productive_id, "fingerprint": fp, "date": attrs["date"]}
            save_json(state_file, state)  # save after every write so a crash can't cause duplicates

    if args.delete_missing:
        # Entries previously synced for this window that no longer exist in FreshBooks.
        # Note: an entry whose date was moved outside the window is also removed here;
        # it gets recreated when you sync the window it moved to.
        lo, hi = args.since.isoformat(), args.until.isoformat()
        for fb_id, rec in list(state.items()):
            if fb_id not in seen and lo <= rec["date"] <= hi:
                try:
                    prod.delete_time_entry(rec["productive_id"])
                except APIError as err:
                    log.error("Failed to delete Productive entry %s: %s", rec["productive_id"], err)
                    stats["errors"] += 1
                    continue
                stats["deleted"] += 1
                if not args.dry_run:
                    del state[fb_id]
                    save_json(state_file, state)

    log.info("Done%s: %s", " (dry run)" if args.dry_run else "", dict(stats) or "nothing to do")
    return 1 if stats["errors"] else 0


# --------------------------------------------------------------------------- other commands

def cmd_auth(_args):
    fb = FreshBooksClient()
    print("1. Open this URL and approve access:\n")
    print("   " + fb.authorize_url() + "\n")
    print("2. You'll be redirected to your redirect URI with ?code=... in the address bar.")
    code = input("3. Paste the code here: ").strip()
    fb.exchange_code(code)
    print(f"Saved tokens to {fb.token_file}. Business ID: {fb.business_id()}")


def cmd_discover(args):
    if args.productive:
        prod = ProductiveClient()
        people, _ = prod.list_all("/people")
        print("\nProductive people (id  name  email)")
        for p in people:
            a = p["attributes"]
            print(f'  {p["id"]:>8}  {a.get("first_name", "")} {a.get("last_name", "")}  {a.get("email", "")}')
        services, included = prod.list_all("/services", {"include": "deal"})
        print("\nProductive services (id  service  /  deal)")
        for s in services:
            deal_ref = (s.get("relationships", {}).get("deal", {}) or {}).get("data") or {}
            deal = included.get(("deals", deal_ref.get("id")), {}).get("attributes", {})
            print(f'  {s["id"]:>8}  {s["attributes"].get("name")}  /  {deal.get("name", "?")}')
        return

    tz = ZoneInfo(env("SYNC_TIMEZONE", "UTC", required=False))
    fb = FreshBooksClient()
    business_id = fb.business_id()
    start_utc, end_utc = utc_range(args.since, args.until, tz)
    combos = defaultdict(lambda: [0, 0])
    sample = None
    for e in fb.time_entries(business_id, start_utc, end_utc):
        sample = sample or e
        key = (e.get("identity_id"), e.get("client_id"), e.get("project_id"), e.get("service_id"))
        combos[key][0] += 1
        combos[key][1] += int(e.get("duration") or 0)

    print(f"\nFreshBooks combinations {args.since}..{args.until}")
    print(f'  {"identity_id":>12} {"client_id":>10} {"project_id":>11} {"service_id":>11} {"entries":>8} {"hours":>7}')
    for (ident, client, project, service), (count, secs) in sorted(combos.items(), key=str):
        print(f"  {ident!s:>12} {client!s:>10} {project!s:>11} {service!s:>11} {count:>8} {secs / 3600:>7.2f}")
    if args.raw and sample:
        print("\nSample raw entry:\n" + json.dumps(sample, indent=2))


# --------------------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="Sync FreshBooks time entries to Productive.io")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="One-time FreshBooks OAuth setup")

    today = date.today()
    for name in ("sync", "discover"):
        p = sub.add_parser(name)
        p.add_argument("--since", type=date.fromisoformat, default=today - timedelta(days=7),
                       help="First local date to include (default: 7 days ago)")
        p.add_argument("--until", type=date.fromisoformat, default=today,
                       help="Last local date to include (default: today)")
        if name == "sync":
            p.add_argument("--dry-run", action="store_true", help="Log what would change without writing")
            p.add_argument("--delete-missing", action="store_true",
                           help="Delete Productive entries whose FreshBooks entry was deleted")
        else:
            p.add_argument("--productive", action="store_true", help="List Productive people and services instead")
            p.add_argument("--raw", action="store_true", help="Also print one raw FreshBooks entry")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    handlers = {"auth": cmd_auth, "sync": cmd_sync, "discover": cmd_discover}
    sys.exit(handlers[args.command](args) or 0)


if __name__ == "__main__":
    main()
