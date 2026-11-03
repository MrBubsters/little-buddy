# Install on Windows

For Windows 10 or 11. An NVIDIA GPU is recommended (see [Choosing a model](../README.md#choosing-a-model)),
but it also runs on CPU, just more slowly.

Before you start, finish the [Discord setup](discord-setup.md) so you have a bot token and your user ID.

---

## 1. Install the prerequisites
Open **PowerShell** (Start menu → type "PowerShell") and run:

```powershell
winget install Python.Python.3.12
winget install Git.Git
winget install Ollama.Ollama
```

Or download the installers manually: [Python](https://www.python.org/downloads/) (tick
**"Add python.exe to PATH"** during install), [Git](https://git-scm.com/download/win),
[Ollama](https://ollama.com/download).

**Close and reopen PowerShell** afterwards so the new commands are found. Check:
```powershell
python --version   # 3.11 or newer
git --version
ollama --version
```

If you have an NVIDIA GPU, make sure its driver is up to date (GeForce Experience / NVIDIA App, or
https://www.nvidia.com/drivers).

## 2. Download the AI model
Ollama runs in the background (tray icon). Download a model; see the
[model table](../README.md#choosing-a-model) to pick one for your hardware:
```powershell
ollama pull qwen3:8b
```
This downloads ~5 GB. Quick test (Ctrl+D or `/bye` to exit):
```powershell
ollama run qwen3:8b "Say hi in five words"
```

## 3. Get the code
Choose a folder (e.g. `Documents`) and clone:
```powershell
cd $HOME\Documents
git clone https://github.com/MrBubsters/little-buddy.git
cd little-buddy
```
> Prefer a folder **outside** OneDrive/Dropbox if you can, since cloud sync can lock the database file.

## 4. Create the Python environment
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```
This creates a private Python install in `.venv` with everything the bot needs.

> **Always run the project with `.venv\Scripts\python`**, not plain `python`. Plain `python` is the
> system install, which doesn't have these packages, and on Windows fails with
> `No time zone found with key …` because Windows lacks the time zone database (`tzdata`).
>
> **VS Code users:** `Ctrl+Shift+P` → **Python: Select Interpreter** → `.venv\Scripts\python.exe`.
> New terminals will then use the venv automatically.

## 5. Configure
```powershell
copy .env.example .env
notepad .env
```
Fill in at least:
```
DISCORD_TOKEN=...      # from the Discord setup
OWNER_ID=...           # your Discord user ID
TIMEZONE=America/Chicago
```
Every other setting is explained in comments inside the file.

## 6. Google Calendar (optional)
Follow [Google Calendar setup](google-calendar-setup.md). In short: put `credentials.json` in
`data\`, run `.venv\Scripts\python gcal.py`, approve in the browser, set `GCAL_ENABLED=true`.

## 7. Run
Double-click **`start.bat`** in the project folder, or:
```powershell
.venv\Scripts\python bot.py
```
You should see:
```
Logged in as Buddy#1234 — model qwen3:8b @ http://localhost:11434
```
DM the bot `!help`. Keep the window open; closing it stops the bot.

## 8. Start automatically (optional)
To launch the bot whenever you log in:
1. Start menu → **Task Scheduler** → **Create Task…** (right panel).
2. **General:** name it `little-buddy`.
3. **Triggers → New:** *Begin the task:* **At log on** → OK.
4. **Actions → New:** *Program/script:* browse to `start.bat` in the project folder.
   *Start in:* the project folder path (e.g. `C:\Users\you\Documents\little-buddy`). → OK.
5. **Conditions:** untick *Start the task only if the computer is on AC power* (laptops).
6. OK. Test it: right-click the task → **Run**.

## Updating
```powershell
cd path\to\little-buddy
git pull
.venv\Scripts\python -m pip install -r requirements.txt
```
Then restart the bot (close the window and run `start.bat` again). After updates, compare your `.env`
with `.env.example` for new settings.
