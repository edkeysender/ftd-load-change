"""
FTD Mode Switcher - FastAPI app
Serves the web UI and JSON API for managing the active mode (training/development).
"""
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

STATE_FILE = Path("/home/ftd/state.json")
VALID_MODES = {"training", "development"}

app = FastAPI(title="FTD Mode Switcher")


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"current_mode": "training"}
    return json.loads(STATE_FILE.read_text())


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


class ModeChange(BaseModel):
    mode: str


@app.get("/api/state")
def get_state():
    """Polled by Windows agents every 15s."""
    return read_state()


@app.post("/api/mode")
def set_mode(change: ModeChange):
    """Switch the active mode. Called by the web UI."""
    if change.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Must be one of: {VALID_MODES}")
    state = read_state()
    state["current_mode"] = change.mode
    write_state(state)
    return state


@app.get("/", response_class=HTMLResponse)
def home():
    state = read_state()
    current = state["current_mode"]

    # Determine the "other" mode for the switch button
    other_mode = "development" if current == "training" else "training"

    # Color theme based on mode
    if current == "training":
        accent = "#1d9e75"   # teal
        accent_bg = "#e1f5ee"
    else:
        accent = "#ba7517"   # amber
        accent_bg = "#faeeda"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FTD Mode Switcher</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 0;
    background: #f1efe8;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }}
  .container {{
    background: white;
    border-radius: 16px;
    padding: 48px;
    max-width: 480px;
    width: 90%;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  h1 {{ margin: 0 0 8px 0; font-size: 22px; font-weight: 500; color: #2c2c2a; }}
  .subtitle {{ color: #5f5e5a; font-size: 14px; margin-bottom: 32px; }}
  .mode-display {{
    background: {accent_bg};
    border: 1px solid {accent};
    border-radius: 12px;
    padding: 32px;
    text-align: center;
    margin-bottom: 24px;
  }}
  .mode-label {{
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: {accent};
    margin-bottom: 8px;
  }}
  .mode-name {{
    font-size: 32px;
    font-weight: 500;
    color: {accent};
    text-transform: uppercase;
  }}
  button {{
    width: 100%;
    padding: 16px;
    font-size: 16px;
    font-weight: 500;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    background: #2c2c2a;
    color: white;
    transition: opacity 0.2s;
  }}
  button:hover {{ opacity: 0.85; }}
  button:disabled {{ opacity: 0.5; cursor: wait; }}
  .footer {{
    margin-top: 24px;
    padding-top: 24px;
    border-top: 1px solid #d3d1c7;
    font-size: 12px;
    color: #888780;
    text-align: center;
  }}
  .status {{ margin-top: 12px; font-size: 13px; text-align: center; min-height: 18px; }}
  .status.ok {{ color: #1d9e75; }}
  .status.err {{ color: #e24b4a; }}
</style>
</head>
<body>
<div class="container">
  <h1>FTD Mode Switcher</h1>
  <div class="subtitle">Active mode is deployed to all Windows clients within ~15 seconds.</div>

  <div class="mode-display">
    <div class="mode-label">Current mode</div>
    <div class="mode-name" id="current-mode">{current}</div>
  </div>

  <button id="switch-btn" onclick="switchMode('{other_mode}')">
    Switch to {other_mode.upper()}
  </button>

  <div class="status" id="status"></div>

  <div class="footer">
    Polling clients fetch from <code>/api/state</code>
  </div>
</div>

<script>
async function switchMode(newMode) {{
  const btn = document.getElementById('switch-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Switching...';
  status.className = 'status';
  try {{
    const res = await fetch('/api/mode', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ mode: newMode }})
    }});
    if (!res.ok) throw new Error('Server returned ' + res.status);
    status.textContent = 'Switched. Reloading...';
    status.className = 'status ok';
    setTimeout(() => location.reload(), 600);
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
    status.className = 'status err';
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""
