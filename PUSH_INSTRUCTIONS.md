# Push these files to your repo

Your repo: **https://github.com/edkeysender/ftd-load-change**

All paths and URLs in the code are already filled in for your repo. No edits needed.

## 1. Unzip the bundle locally

Unzip and `cd` into the folder so you're in the directory that contains `install.sh`, `README.md`, `rpi/`, `windows/`.

## 2. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/edkeysender/ftd-load-change.git
git push -u origin main
```

If GitHub prompts for credentials: username is your GitHub username, password is a Personal Access Token (Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token with `repo` scope).

## 3. One-line install on the RPi

SSH into your RPi and run:

```bash
curl -fsSL https://raw.githubusercontent.com/edkeysender/ftd-load-change/main/install.sh | sudo bash
```

When it finishes, it prints the web UI URL (something like `http://192.168.x.x:8080`).

## 4. Verify

In a browser: open the printed URL. You should see the mode switcher with "TRAINING" highlighted in teal.

From any LAN machine:
```bash
curl http://<rpi-ip>:8080/api/state
# Expected: {"current_mode":"training"}
```

## 5. Next steps

Continue with the rest of `README.md`:
- **SSH keys for Windows clients** — give each Windows machine SSH access to the RPi
- **Set up each Windows machine** — install the sync agent and scheduled task

## Future updates

When you change code and push to GitHub:

```bash
# On the RPi:
sudo /opt/ftd-load-change/update.sh
```

This pulls the latest from GitHub and re-runs setup. The setup script is idempotent — safe to re-run any number of times. The systemd service restarts automatically with the new app.py.
