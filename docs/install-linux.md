# Install on Linux

Written for **Ubuntu / Debian** (incl. Linux Mint, Pop!_OS). For other distros, swap the `apt`
commands for your package manager. Works on a desktop or a headless server reached via SSH.
An NVIDIA GPU is recommended (see [Choosing a model](../README.md#choosing-a-model)); CPU works but is slower.

Before you start, finish the [Discord setup](discord-setup.md) so you have a bot token and your user ID.

---

## 1. NVIDIA driver (GPU users)
Check whether it's already working:
```bash
nvidia-smi
```
If that prints a table with your GPU, skip ahead. Otherwise:
```bash
sudo apt update
sudo ubuntu-drivers install
sudo reboot
```
Run `nvidia-smi` again after the reboot.

## 2. Install Ollama
```bash
curl -fsSL https://ollama.com/install.sh | sh
```
This installs Ollama as a background service that starts at boot. Download a model; see the
[model table](../README.md#choosing-a-model) to pick one for your hardware:
```bash
ollama pull qwen3:8b
ollama run qwen3:8b "Say hi in five words"    # quick test
```

## 3. Install Python and Git
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version    # 3.10 or newer
```

## 4. Get the code and create the environment
```bash
cd ~
git clone https://github.com/MrBubsters/little-buddy.git
cd little-buddy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 5. Configure
```bash
cp .env.example .env
nano .env       # Ctrl+O, Enter to save; Ctrl+X to exit
```
Fill in at least `DISCORD_TOKEN`, `OWNER_ID` and `TIMEZONE`. To find your time zone name:
`timedatectl | grep "Time zone"`.

Moving from another computer? Copy your existing `.env` and `data/` instead; see
[Moving to another computer](moving-machines.md).

## 6. Google Calendar (optional)
Follow [Google Calendar setup](google-calendar-setup.md).

The one-time authorization opens a web browser. On a **desktop**, run
`.venv/bin/python gcal.py` here as described. On a **headless server**, do the authorization on any
computer with a browser (e.g. your Windows/Mac machine with the project cloned), then copy
`data/credentials.json` and `data/token.json` into `~/little-buddy/data/` on the server.

Then set `GCAL_ENABLED=true` in `.env`.

## 7. Test run
```bash
bash start.sh
```
You should see `Logged in as Buddy#1234 — model qwen3:8b @ http://localhost:11434`.
DM the bot `!help`, then press **Ctrl+C** to stop.

## 8. Run as a service (starts at boot, restarts on crash)
From the project folder:
```bash
sed "s|__USER__|$USER|; s|__DIR__|$PWD|g" deploy/little-buddy.service \
  | sudo tee /etc/systemd/system/little-buddy.service
sudo systemctl daemon-reload
sudo systemctl enable --now little-buddy
```
This fills in your username and folder, installs the service, and starts it.

Everyday commands:
```bash
systemctl status little-buddy          # running?
journalctl -u little-buddy -f          # live logs (Ctrl+C to stop watching)
sudo systemctl restart little-buddy    # after changing .env or updating
sudo systemctl stop little-buddy       # stop
sudo systemctl disable little-buddy    # don't start at boot
```

## Updating
```bash
cd ~/little-buddy
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart little-buddy
```
After updates, compare your `.env` with `.env.example` for new settings.

---

## macOS
The Linux steps work on macOS with these swaps:
- Install tools with [Homebrew](https://brew.sh): `brew install python git` and the Ollama app from https://ollama.com/download (uses Apple-silicon GPU automatically).
- Skip the NVIDIA and systemd steps; run with `bash start.sh` (or create a launchd agent / login item).
