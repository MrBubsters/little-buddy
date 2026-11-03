# Google Calendar setup (optional)

Two-way sync between the bot and your Google Calendar:

- **Google → bot:** every few minutes the bot reads the next 30 days of events. They appear in your
  agenda, digest and reminders (marked 📆). Changes and deletions in Google are picked up.
- **Bot → Google:** events you tell the bot about ("dinner with Sam Friday 7pm") are created in
  Google Calendar. Edits and deletes through the bot go to Google too.
- Tasks, deadlines and reminders stay in the bot only.

This uses Google's free Calendar API with your **own** Google Cloud project. Nothing is shared with
anyone else. It takes about 10 minutes, and you end up with two files in `data/`:

| File | What it is | Secret? |
|---|---|---|
| `data/credentials.json` | Identifies *your app* to Google (downloaded once) | Yes |
| `data/token.json` | Your permission for the app to use your calendar (created on first login) | Yes, like a password |

Both are git-ignored. Never commit or share them.

---

## 1. Create a Google Cloud project
1. Go to https://console.cloud.google.com and sign in with the Google account whose calendar you want.
2. Accept the terms if prompted.
3. Click the **project picker** (top-left, next to the Google Cloud logo) → **New Project**.
4. **Project name:** anything, e.g. `little-buddy`.
5. **Organization / Location:** choose **No organization**.
   > If you're signed into a work/school account, the project may default to your company's
   > organization. That causes `Error 403: org_internal` later. Use a personal Gmail account
   > and **No organization**.
6. Click **Create**, then make sure the new project is selected in the project picker.

## 2. Enable the Google Calendar API
1. Open the menu (☰) → **APIs & Services** → **Library**.
2. Search for **Google Calendar API** → click it → **Enable**.

## 3. Configure the consent screen
This is the "Allow access?" page you'll see when logging in.

1. Menu (☰) → **Google Auth Platform** (older consoles: **APIs & Services → OAuth consent screen**).
2. If you see **Get started**, click it and fill in:
   - **App name:** e.g. `little-buddy`
   - **User support email:** your email
   - **Audience:** **External** ← important
   - **Contact information:** your email
   - Agree to the policy → **Create**.
3. Go to **Audience** (left sidebar):
   - Check **User type** says **External**. If it says Internal, click **Make external**.
   - Under **Publishing status**, click **Publish app** → **Confirm**. It should now say **In production**.

> **Why publish?** While an app is in *Testing*, Google only lets listed test users sign in **and**
> expires the login every **7 days**, so sync silently stops each week. Publishing removes both
> limits. You do **not** need to submit for verification. It's your private app, used only by you.
>
> Prefer to stay in Testing? Under **Test users** click **Add users** and add your Gmail address,
> and expect to rerun step 5 weekly.

## 4. Create the OAuth client → `data/credentials.json`
1. Left sidebar → **Clients** (older consoles: **APIs & Services → Credentials → Create credentials → OAuth client ID**).
2. Click **Create client**.
3. **Application type:** **Desktop app**. **Name:** anything (e.g. `little-buddy desktop`).
4. Click **Create**, then **Download JSON** in the dialog (or the ⬇ icon next to the client in the list).
5. Rename the downloaded file (`client_secret_….json`) to **`credentials.json`** and move it into
   the project's **`data/`** folder (create the folder if it doesn't exist):
   ```
   little-buddy/
   └── data/
       └── credentials.json
   ```

## 5. Authorize (one time) → `data/token.json`
This step needs a computer **with a web browser**. If your bot runs on a headless Linux server, do
this on your desktop and copy `data/token.json` over afterwards (step 7).

From the project folder, with the virtual environment's Python:

```bash
# Windows
.venv\Scripts\python gcal.py
# Linux / macOS
.venv/bin/python gcal.py
```

1. A browser tab opens. Choose your Google account.
2. You'll see **"Google hasn't verified this app"**. That's expected for a personal app.
   Click **Advanced** → **Go to *(your app name)* (unsafe)**.
3. Click **Continue** / **Allow** to grant calendar access.
4. The tab says *"The authentication flow has completed"*. Back in the terminal you'll see:
   ```
   Authorized. Token saved to .../data/token.json
     2026-11-07T12:00 - Lunch with Alex
     ...
   ```
   (a list of your upcoming events). That confirms it works.

## 6. Turn on sync
In `.env`:
```
GCAL_ENABLED=true
```
Restart the bot. In Discord, send `!sync` and you should get "📆 Synced N events from Google Calendar."

> If your `.env` was created from an older `.env.example`, it may not contain the `GCAL_` lines at
> all. Copy them over from the current `.env.example`.

## 7. Using it on another machine
Copy both `data/credentials.json` and `data/token.json` to the other machine's `data/` folder.
No need to authorize again: the token refreshes itself.

---

## Options
| `.env` key | Default | Meaning |
|---|---|---|
| `GCAL_CALENDAR_ID` | `primary` | Which calendar. For another one: Google Calendar → ⚙ Settings → pick the calendar → *Integrate calendar* → **Calendar ID** |
| `GCAL_SYNC_DAYS` | `30` | How far ahead to sync |
| `GCAL_SYNC_MINUTES` | `5` | How often to sync |
| `GCAL_EVENT_MINUTES` | `60` | Length of new events created by the bot |
| `GCAL_WORK_KEYWORDS` | empty | Title words that tag Google events as **work** (hidden on days off), e.g. `standup,sprint` |

## Troubleshooting
| Message / symptom | Cause → fix |
|---|---|
| `Error 403: org_internal` ("can only be used within its organization") | Consent screen is **Internal**. Step 3: **Make external**. If that's greyed out, the project belongs to a company org; create a new project with **No organization** |
| `Error 403: access_denied` ("has not completed the Google verification process") | App is in **Testing** and you're not a test user. Step 3: **Publish app**, or add yourself under **Test users** |
| Sync stopped working after ~7 days (`invalid_grant`) | App was in Testing. Publish it, delete `data/token.json`, rerun step 5 |
| `Missing data/credentials.json` | Step 4: file must be named exactly `credentials.json` and be inside `data/` |
| `Google Calendar not authorized. Run: …gcal.py` | `data/token.json` missing; do step 5 |
| `No time zone found with key …` | You ran system Python. Use `.venv\Scripts\python` / `.venv/bin/python` |
| Events created through the bot don't appear in Google | `GCAL_ENABLED=true` missing from `.env`, or the bot wasn't restarted. Events saved while sync was off are uploaded automatically on the next sync |
| `!sync` says "isn't set up" | Same as above: check `GCAL_ENABLED` and that `data/token.json` exists |

To revoke access at any time: https://myaccount.google.com/permissions → your app → **Remove access**.

## Limitations
- One calendar is synced.
- Editing or deleting a **recurring** event through the bot affects only that occurrence; change the
  whole series in Google Calendar.
