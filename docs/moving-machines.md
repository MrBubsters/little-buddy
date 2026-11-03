# Moving to another computer

The code comes from git, but **your data and secrets don't**. They're git-ignored on purpose:

| Path | Contains |
|---|---|
| `.env` | Discord token, your settings |
| `data/jarofass.db` | Everything the bot knows: items, facts, chat history, work log |
| `data/credentials.json` | Google app credentials (if using Google Calendar) |
| `data/token.json` | Google login token (if using Google Calendar) |

## Steps
1. **Stop the bot on the old computer** (close the window / `sudo systemctl stop little-buddy`).
   Two copies running at once means double reminders and conflicting calendar writes.
2. **Install on the new computer** ([Windows](install-windows.md) or [Linux](install-linux.md)) up
   to the *Configure* step. Skip creating `.env`; you'll copy it.
3. **Copy the files** into the same places in the new project folder. Any method works (USB stick,
   network share, cloud drive). Over the network with `scp` (built into Windows 10+, macOS, Linux):

   **Windows → Linux** (run in PowerShell, in the old project folder):
   ```powershell
   ssh user@new-pc "mkdir -p ~/little-buddy/data"
   scp .env user@new-pc:~/little-buddy/
   scp data\jarofass.db data\credentials.json data\token.json user@new-pc:~/little-buddy/data/
   ```
   **Linux → Linux:**
   ```bash
   scp -r .env data user@new-pc:~/little-buddy/
   ```
   Replace `user@new-pc` with the login and hostname/IP of the new machine.
4. **Check settings** in the copied `.env`: `TIMEZONE`, and `OLLAMA_MODEL` if the new machine has a
   different GPU (see [Choosing a model](../README.md#choosing-a-model)). Make sure that model is
   pulled: `ollama pull <model>`.
5. **Start the bot** and DM it `!agenda` to confirm your items came across.

## Keeping the old computer as a backup
Copy `data/jarofass.db` somewhere safe occasionally (stop the bot first for a clean copy). It's a
single file, and restoring it restores everything.
