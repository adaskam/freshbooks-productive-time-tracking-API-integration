# User Guide: FreshBooks → Productive Time Sync

This tool copies time entries from FreshBooks into Productive.io. It is one-way:
it reads from FreshBooks and writes to Productive, and never changes anything in
FreshBooks.

Setup takes about 20–30 minutes and is done once. After that, syncing is a single
command, which you can also schedule to run automatically.

**Contents**

1. [Before you start](#1-before-you-start)
2. [Install](#2-install)
3. [Create a FreshBooks app](#3-create-a-freshbooks-app)
4. [Get your Productive API token](#4-get-your-productive-api-token)
5. [Configure `.env`](#5-configure-env)
6. [Connect to FreshBooks](#6-connect-to-freshbooks)
7. [Find the IDs you need](#7-find-the-ids-you-need)
8. [Create `mapping.json`](#8-create-mappingjson)
9. [Do a test run](#9-do-a-test-run)
10. [Run the sync](#10-run-the-sync)
11. [Automate it](#11-automate-it)
12. [Command reference](#12-command-reference)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Before you start

You need:

- **Python 3.9 or newer.** Check with `python3 --version`.
- **A FreshBooks account** that can see the time entries you want to sync.
- **A Productive.io account** that can create time entries for the people you'll
  sync.
- **A web address you control** to use as the FreshBooks "redirect URI". It just
  needs to start with `https://`; nothing has to run there. You only copy a code
  out of the browser's address bar after being sent to it.

> **Older Python?** On Ubuntu you can install a newer version alongside the system
> one:
> ```
> sudo add-apt-repository ppa:deadsnakes/ppa
> sudo apt update
> sudo apt install python3.11 python3.11-venv
> ```
> Then use `python3.11` wherever this guide says `python3`.

## 2. Install

From the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run `source .venv/bin/activate` again each time you open a new terminal. While the
virtual environment is active, `python` refers to the right version, and the rest
of this guide uses `python`.

## 3. Create a FreshBooks app

1. Go to <https://my.freshbooks.com/#/developer> and create a new app.
2. Under **Redirect URIs**, add your https address, for example
   `https://example.com/callback`.
3. Give the app permission to read time entries and user profile information.
4. Save the app and note its **Client ID** and **Client Secret**.

## 4. Get your Productive API token

1. In Productive, go to **Settings → API integrations**.
2. Generate a token with **read and write** access. The tool creates, updates and
   deletes time entries.
3. Note the **token** and your **Organization ID**. Both are shown on that page.

> Time entries are created as the people you map in step 8, so the token must
> belong to someone allowed to log time for them, such as an admin.

## 5. Configure `.env`

```bash
cp .env.example .env
```

Open `.env` and fill it in:

| Setting | What to put there |
|---|---|
| `FRESHBOOKS_CLIENT_ID` | From step 3 |
| `FRESHBOOKS_CLIENT_SECRET` | From step 3 |
| `FRESHBOOKS_REDIRECT_URI` | Exactly the same address you registered in step 3 |
| `FRESHBOOKS_BUSINESS_ID` | Leave blank. Only needed if your login has access to more than one business (the tool will tell you) |
| `PRODUCTIVE_API_TOKEN` | From step 4 |
| `PRODUCTIVE_ORG_ID` | From step 4 |
| `SYNC_TIMEZONE` | Your team's timezone, e.g. `America/Chicago`. Used to decide which day an entry belongs to |
| `TAG_NOTES` | `true` adds `[FreshBooks #12345]` to each Productive note, which makes it easy to trace an entry back. Set `false` to turn this off |

`.env` is already listed in `.gitignore`, so your credentials won't be committed.

## 6. Connect to FreshBooks

```bash
python sync.py auth
```

1. The tool prints a link. Open it in your browser and approve access.
2. FreshBooks sends you to your redirect URI with `?code=...` on the end of the
   address. The page itself may show an error; that's fine.
3. Copy everything after `code=` and paste it into the terminal.

The tool saves your login to `freshbooks_tokens.json` and prints your business ID.
You only do this once. The tool keeps the login fresh on its own after that.

> **Important:** FreshBooks login tokens can only be used once, and each refresh
> replaces the last one. Don't copy `freshbooks_tokens.json` to another machine
> while it's still in use here, and don't run two syncs at the same time, or one
> of them will lose its login and you'll need to run `auth` again.

## 7. Find the IDs you need

The mapping file in step 8 connects FreshBooks IDs to Productive IDs. These two
commands list them.

**FreshBooks side.** Pick a date range that has typical time entries:

```bash
python sync.py discover --since 2026-09-01 --until 2026-09-30
```

```
FreshBooks combinations 2026-09-01..2026-09-30
   identity_id  client_id  project_id  service_id  entries   hours
        123456        333         111         222       14   31.50
        123456        333         111        None        3    4.00
        234567        444         112         222        9   18.25
```

- `identity_id` is the person who logged the time.
- `client_id`, `project_id` and `service_id` are what the time was logged against.

Add `--raw` to also print one full entry, which helps you tell which ID is which.

**Productive side:**

```bash
python sync.py discover --productive
```

This lists every Productive person (ID, name, email) and every service (ID,
service name and the deal or budget it belongs to).

## 8. Create `mapping.json`

```bash
cp mapping.example.json mapping.json
```

The file has two parts.

### People: who logged the time

```json
"people": {
  "123456": "987654",
  "234567": "987655"
},
"default_person_id": null
```

Each line is `"FreshBooks identity_id": "Productive person ID"`. Match people up by
name or email using the two `discover` outputs.

`default_person_id` is used for anyone not listed. Leave it `null` to skip
unlisted people; they're reported as *unmapped* instead.

### Services: where the time goes

```json
"services": [
  { "fb_project_id": 111, "fb_service_id": 222, "productive_service_id": "5550001" },
  { "fb_project_id": 111,                        "productive_service_id": "5550002" },
  { "fb_client_id": 333,                         "productive_service_id": "5550003" }
],
"default_service_id": null
```

Each rule matches on any combination of `fb_client_id`, `fb_project_id` and
`fb_service_id`, and sends matching entries to `productive_service_id`. When more
than one rule matches, the **most specific rule wins**. With the rules above:

| FreshBooks entry | Goes to | Why |
|---|---|---|
| project 111, service 222 | 5550001 | Matches both fields of the first rule |
| project 111, service 999 | 5550002 | Only the project matches |
| client 333, another project | 5550003 | Only the client matches |
| anything else | `default_service_id` | Skipped if that's `null` |

A simple setup is often one rule per FreshBooks project.

## 9. Do a test run

```bash
python sync.py sync --since 2026-09-01 --until 2026-09-30 --dry-run
```

A dry run shows what would be created without changing anything in Productive.
Check for:

- **`Unmapped entry ...` warnings.** Add the IDs shown in the warning to
  `mapping.json`, then run the dry run again.
- **The summary line at the end**, for example:
  `Done (dry run): {'created': 42, 'skipped': 2, 'unmapped': 1}`

Add `-v` to see why each skipped entry was skipped.

## 10. Run the sync

When the dry run looks right, run the same command without `--dry-run`:

```bash
python sync.py sync --since 2026-09-01 --until 2026-09-30
```

From now on, a plain

```bash
python sync.py sync
```

syncs the last 7 days. It's safe to run as often as you like:

- **New** FreshBooks entries are created in Productive.
- **Edited** entries (hours, date, note, person or service) are updated in
  Productive.
- **Unchanged** entries are left alone, so running it again never creates
  duplicates.
- An entry someone **deleted in Productive** is recreated from FreshBooks.

**What gets skipped:**

- Entries whose timer is still running. They sync on a later run once stopped.
- Entries not yet logged in FreshBooks.
- Entries of 30 seconds or less. Time is rounded to whole minutes.
- Entries with no person or service mapping.

**Entries deleted in FreshBooks** aren't removed from Productive unless you ask:

```bash
python sync.py sync --delete-missing
```

This removes Productive entries, within the date range, whose FreshBooks entry no
longer exists. If an entry's date was moved outside the range, it's removed here
too, then recreated when you sync the range it moved to.

## 11. Automate it

To run the sync every night at 2am, run `crontab -e` and add this line (use your
own paths):

```
0 2 * * * cd /path/to/project && .venv/bin/python sync.py sync >> sync.log 2>&1
```

Because each run re-checks the last 7 days, entries edited late in FreshBooks are
still picked up. Check `sync.log` now and then for `ERROR` or `Unmapped` lines,
especially when new people or projects are added in FreshBooks.

## 12. Command reference

| Command | What it does |
|---|---|
| `python sync.py auth` | Connect to FreshBooks (once) |
| `python sync.py discover [--since D] [--until D] [--raw]` | List FreshBooks IDs and hours |
| `python sync.py discover --productive` | List Productive people and services |
| `python sync.py sync [--since D] [--until D]` | Sync entries (default: last 7 days) |
| `  --dry-run` | Preview only; change nothing |
| `  --delete-missing` | Also remove entries deleted in FreshBooks |
| `-v` (before the command, e.g. `python sync.py -v sync`) | Show detailed logs |

Dates are `YYYY-MM-DD`, in your `SYNC_TIMEZONE`, and both ends are included.

### Files the tool creates

| File | Purpose | Safe to delete? |
|---|---|---|
| `freshbooks_tokens.json` | FreshBooks login | Yes, but you'll need to run `auth` again |
| `sync_state.json` | Records which FreshBooks entry became which Productive entry | **No**, see below |
| `sync.log` | Log output, if you set up cron | Yes |

> **Don't delete `sync_state.json`.** It's how the tool knows an entry was
> already synced. Without it, the next sync creates every entry in the range
> again, and you'll have duplicates in Productive to clean up by hand.

## 13. Troubleshooting

| Message | Fix |
|---|---|
| `Missing required environment variable: X` | Fill in `X` in `.env`, and make sure you're running from the project folder |
| `No FreshBooks tokens found. Run: python sync.py auth` | Run `auth` (step 6) |
| `... /auth/oauth/token -> 400` or `401` | Your FreshBooks login expired or its token was already used. Run `auth` again |
| `Set FRESHBOOKS_BUSINESS_ID to one of: ...` | Your login has access to several businesses. Copy the right ID into `.env` |
| `Mapping file not found` | Create `mapping.json` (step 8) |
| `Unmapped entry ...` | Add that person or project to `mapping.json` |
| `ModuleNotFoundError: No module named 'zoneinfo'` | Your Python is older than 3.9 (see step 1) |
| `ModuleNotFoundError: No module named 'requests'` | Activate the virtual environment: `source .venv/bin/activate` |
| `Failed to sync FreshBooks entry ...: ... 422` | Productive rejected the entry. Usually the person isn't allowed to log time on that service, or the service's budget is closed |
| `... returned 429, retrying` | Rate limited. The tool waits and retries on its own; no action needed |

The sync exits with code 1 if any entry failed, and 0 otherwise. Failed entries
are retried on the next run.
