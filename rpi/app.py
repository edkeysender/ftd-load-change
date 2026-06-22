"""
FTD Mode Switcher - FastAPI app

Two axes of control:
  * mode     : training | development  (which git branch clients deploy from)
  * versions : per mode, which version of each software component is active

Repo layout per branch:
  <component>/<version>/...      e.g.  powerSwitchUI/2.0.0/...
A "component" is any top-level folder; a "version" is any subfolder of it.
The Windows agent assembles the selected versions and mirrors their contents,
flattened, into D:\\ftd\\products\\Load_testing.
"""
import html
import json
import re
import subprocess
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

STATE_FILE = Path("/home/ftd/state.json")
REPO_DIR = Path("/home/ftd/repos/ftd.git")
VALID_MODES = ("training", "development")

app = FastAPI(title="FTD Mode Switcher")


# ----------------------------- state -----------------------------

def _default_state() -> dict:
    return {"current_mode": "training",
            "selections": {m: {} for m in VALID_MODES}}


def read_state() -> dict:
    if not STATE_FILE.exists():
        return _default_state()
    state = json.loads(STATE_FILE.read_text())
    state.setdefault("current_mode", "training")
    selections = state.setdefault("selections", {})
    for mode in VALID_MODES:
        selections.setdefault(mode, {})
    return state


def write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------- git inspection -------------------------

def _git(args: list, timeout: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(["git", f"--git-dir={REPO_DIR}"] + args,
                          capture_output=True, text=True, timeout=timeout)


def _version_key(v: str):
    """Natural sort key so 2.0.0 sorts after 10.0.0 correctly."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", v)]


def tree_dirs(ref: str) -> list:
    """Names of subdirectories (git trees) directly under a tree-ish ref.

    ref is a branch ("training") or a branch:path ("training:powerSwitchUI").
    Returns [] on any error (missing branch/path, git unavailable).
    """
    names = []
    try:
        result = _git(["ls-tree", ref])
        if result.returncode != 0:
            return names
        for line in result.stdout.splitlines():
            meta, _, name = line.partition("\t")
            parts = meta.split()
            if len(parts) >= 2 and parts[1] == "tree" and name:
                names.append(name)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return names


def available(branch: str) -> dict:
    """{component: [versions...]} for a branch, read live from the repo."""
    result = {}
    for component in sorted(tree_dirs(branch), key=str.lower):
        result[component] = sorted(tree_dirs(f"{branch}:{component}"), key=_version_key)
    return result


# ------------------------------ API ------------------------------

class ModeChange(BaseModel):
    mode: str


class VersionSelect(BaseModel):
    mode: str
    component: str
    version: str


@app.get("/api/state")
def get_state():
    """Polled by Windows agents every 15s. Returns the active mode and the
    component->version map the agent should deploy for that mode."""
    state = read_state()
    mode = state["current_mode"]
    return {"current_mode": mode, "versions": state["selections"].get(mode, {})}


@app.get("/api/components")
def get_components():
    """Available components and versions per branch (for the UI / debugging)."""
    return {mode: available(mode) for mode in VALID_MODES}


@app.post("/api/mode")
def set_mode(change: ModeChange):
    """Switch the active mode. Called by the web UI."""
    if change.mode not in VALID_MODES:
        raise HTTPException(400, f"Invalid mode. Must be one of: {VALID_MODES}")
    state = read_state()
    state["current_mode"] = change.mode
    write_state(state)
    return get_state()


@app.post("/api/version")
def set_version(sel: VersionSelect):
    """Set the active version of a component for a given mode."""
    if sel.mode not in VALID_MODES:
        raise HTTPException(400, f"Invalid mode. Must be one of: {VALID_MODES}")
    avail = available(sel.mode)
    if sel.component not in avail:
        raise HTTPException(404, f"No component '{sel.component}' on branch '{sel.mode}'")
    if sel.version not in avail[sel.component]:
        raise HTTPException(404,
            f"No version '{sel.version}' for '{sel.component}' on branch '{sel.mode}'")
    state = read_state()
    state["selections"].setdefault(sel.mode, {})[sel.component] = sel.version
    write_state(state)
    return {"mode": sel.mode, "component": sel.component, "version": sel.version}


# ------------------------------ UI -------------------------------

def render_panel(mode: str, avail: dict, selected: dict, is_active: bool) -> str:
    rows = []
    if not avail:
        rows.append('<div class="empty">No software on this branch yet.</div>')
    for component, versions in avail.items():
        current = selected.get(component, "")
        if not versions:
            control = '<select disabled><option>(no versions)</option></select>'
        else:
            opts = []
            if current not in versions:
                opts.append('<option value="" selected disabled>— select —</option>')
            for v in versions:
                chosen = " selected" if v == current else ""
                opts.append(f'<option value="{html.escape(v)}"{chosen}>{html.escape(v)}</option>')
            control = (f"<select onchange=\"setVersion('{mode}','{html.escape(component)}',this.value)\">"
                       f'{"".join(opts)}</select>')
        rows.append(f'<div class="comp-row"><span class="comp-name">'
                    f'{html.escape(component)}</span>{control}</div>')
    badge = '<span class="badge">active</span>' if is_active else ""
    active_cls = " active" if is_active else ""
    return (f'<div class="panel{active_cls}">'
            f'<div class="panel-head">{mode.upper()}{badge}</div>'
            f'{"".join(rows)}</div>')


@app.get("/", response_class=HTMLResponse)
def home():
    state = read_state()
    current = state["current_mode"]
    other_mode = "development" if current == "training" else "training"

    # Color theme based on mode
    if current == "training":
        accent = "#1d9e75"   # teal
        accent_bg = "#e1f5ee"
    else:
        accent = "#ba7517"   # amber
        accent_bg = "#faeeda"

    panels_html = "".join(
        render_panel(mode, available(mode), state["selections"].get(mode, {}), mode == current)
        for mode in VALID_MODES
    )

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
    max-width: 580px;
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
    font-size: 13px; text-transform: uppercase; letter-spacing: 1px;
    color: {accent}; margin-bottom: 8px;
  }}
  .mode-name {{
    font-size: 32px; font-weight: 500; color: {accent}; text-transform: uppercase;
  }}
  button {{
    width: 100%; padding: 16px; font-size: 16px; font-weight: 500;
    border: none; border-radius: 8px; cursor: pointer;
    background: #2c2c2a; color: white; transition: opacity 0.2s;
  }}
  button:hover {{ opacity: 0.85; }}
  button:disabled {{ opacity: 0.5; cursor: wait; }}
  .footer {{
    margin-top: 24px; padding-top: 24px; border-top: 1px solid #d3d1c7;
    font-size: 12px; color: #888780; text-align: center;
  }}
  .status {{ margin-top: 12px; font-size: 13px; text-align: center; min-height: 18px; }}
  .status.ok {{ color: #1d9e75; }}
  .status.err {{ color: #e24b4a; }}
  .contents {{ margin-top: 28px; }}
  .contents-title {{
    font-size: 13px; text-transform: uppercase; letter-spacing: 1px;
    color: #5f5e5a; margin-bottom: 12px;
  }}
  .panels {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .panel {{
    flex: 1 1 220px; min-width: 0;
    border: 1px solid #d3d1c7; border-radius: 10px;
    padding: 14px; background: #fafaf7;
  }}
  .panel.active {{ border-color: {accent}; background: {accent_bg}; }}
  .panel-head {{
    font-weight: 600; font-size: 13px; letter-spacing: 0.5px;
    color: #2c2c2a; margin-bottom: 12px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .badge {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
    background: {accent}; color: white; padding: 2px 6px; border-radius: 6px;
  }}
  .comp-row {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; margin-bottom: 8px;
  }}
  .comp-name {{
    font-size: 13px; color: #3c3b38; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }}
  .comp-row select {{
    font-size: 13px; padding: 4px 6px; border-radius: 6px;
    border: 1px solid #c8c6bc; background: white; max-width: 120px;
  }}
  .empty {{ font-size: 13px; color: #999; font-style: italic; }}
</style>
</head>
<body>
<div class="container">
  <h1>FTD Mode Switcher</h1>
  <div class="subtitle">Active mode &amp; versions deploy to all Windows clients within ~15 seconds.</div>

  <div class="mode-display">
    <div class="mode-label">Current mode</div>
    <div class="mode-name" id="current-mode">{current}</div>
  </div>

  <button id="switch-btn" onclick="switchMode('{other_mode}')">
    Switch to {other_mode.upper()}
  </button>

  <div class="status" id="status"></div>

  <div class="contents">
    <div class="contents-title">Active version per mode</div>
    <div class="panels">{panels_html}</div>
  </div>

  <div class="footer">
    Clients poll <code>/api/state</code> &middot; versions via <code>/api/components</code>
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

async function setVersion(mode, component, version) {{
  if (!version) return;
  const status = document.getElementById('status');
  status.textContent = 'Setting ' + component + ' = ' + version + ' (' + mode + ')...';
  status.className = 'status';
  try {{
    const res = await fetch('/api/version', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ mode: mode, component: component, version: version }})
    }});
    if (!res.ok) throw new Error('Server returned ' + res.status);
    status.textContent = component + ' → ' + version + ' (' + mode + ') saved. '
      + (mode === '{current}' ? 'Clients update within ~15s.' : 'Applies when ' + mode + ' is active.');
    status.className = 'status ok';
  }} catch (e) {{
    status.textContent = 'Error: ' + e.message;
    status.className = 'status err';
  }}
}}
</script>
</body>
</html>"""
