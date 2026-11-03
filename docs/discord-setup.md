# Discord setup

You'll create your own private Discord bot and collect two values for `.env`:

| `.env` key | What it is |
|---|---|
| `DISCORD_TOKEN` | The bot's password, which lets the program log in as your bot |
| `OWNER_ID` | **Your** Discord user ID. The bot ignores everyone else |

Takes about 5 minutes. You need a normal Discord account.

---

## 1. Create the application
1. Go to the **Discord Developer Portal**: https://discord.com/developers/applications and log in.
2. Click **New Application** (top right).
3. Give it a name (this is the bot's display name, e.g. "Buddy"), accept the terms, click **Create**.
4. Optional: on **General Information**, upload an avatar.

## 2. Get the bot token → `DISCORD_TOKEN`
1. In the left sidebar, click **Bot**.
2. Under **Token**, click **Reset Token** → **Yes, do it!** (enter your 2FA code if asked).
3. Click **Copy**. Paste it into `.env`:
   ```
   DISCORD_TOKEN=paste-the-token-here
   ```
   No quotes, no spaces.

> ⚠️ The token is shown **only once**. If you lose it, click Reset Token again (the old one stops working).
> Treat it like a password: anyone with it can control your bot. If it ever leaks, reset it.

## 3. Enable Message Content Intent (required)
Still on the **Bot** page:
1. Scroll to **Privileged Gateway Intents**.
2. Turn on **Message Content Intent**.
3. Click **Save Changes** at the bottom.

Without this the bot comes online but can't read your messages, so it never replies.

**Optional:** on the same page, turn **Public Bot** off so nobody else can add your bot to their servers.

## 4. Invite the bot to a server
Discord bots can only DM people they share a server with, so the bot must join a server you're in.

If you don't have a server for this, create a private one: in the Discord app, click **+**
(bottom of the server list) → **Create My Own** → **For me and my friends** → name it.

Then:
1. In the Developer Portal, left sidebar → **OAuth2** → **URL Generator**.
2. Under **Scopes**, tick **bot**.
3. Under **Bot Permissions** (appears below), tick:
   - **Send Messages**
   - **Read Message History**
   - **View Channels**
4. Copy the **Generated URL** at the bottom, open it in your browser.
5. Pick your server → **Continue** → **Authorize**.

The bot now appears in the server's member list (offline until you start the program).

## 5. Get your user ID → `OWNER_ID`
1. In the Discord app: **User Settings** (gear icon near your name) → **Advanced**.
2. Turn on **Developer Mode**.
3. Close settings. Right-click **your own name** (in a member list or chat) → **Copy User ID**.
4. Paste into `.env`:
   ```
   OWNER_ID=123456789012345678
   ```

> Make sure this is **your** ID, not the bot's. The bot only responds to this user.

## 6. Allow DMs from the bot
The bot talks to you in direct messages. If your privacy settings block DMs from server members, it can't reach you:

Right-click the server icon → **Privacy Settings** → turn on **Direct Messages**.

## 7. (Optional) Use a channel instead of DMs → `CHANNEL_ID`
If you'd rather chat and get reminders in a server channel:
1. With Developer Mode on, right-click the channel → **Copy Channel ID**.
2. Set `CHANNEL_ID=` to it in `.env`.

The bot then listens and posts in that channel (and still answers DMs). Reminders @mention you
either way so you get notifications.

---

## Test it
Once the program is running (see the install guide for your OS), the terminal shows
`Logged in as YourBot#1234`. Open a DM with the bot (click it in the member list → **Message**) and send:

```
!help
```

| Problem | Fix |
|---|---|
| Program exits with "Set DISCORD_TOKEN and OWNER_ID" | `.env` missing or values empty; check the file is named exactly `.env` |
| `LoginFailure: Improper token` | Token copied wrong or reset since; reset and paste again |
| `PrivilegedIntentsRequired` | Step 3: enable Message Content Intent |
| Bot online but never replies | Wrong `OWNER_ID` (you copied the bot's ID?) or Message Content Intent off |
| Can't open a DM with the bot | Step 4 (share a server) and step 6 (allow DMs) |
