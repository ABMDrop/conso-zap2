import os
import sys
import time
import random
import datetime
import hashlib
import threading
import requests
import json
import base64
from flask import Flask, jsonify, render_template_string, request

from conso_client import ConsoClient, PLATFORM_PRESETS, ACCOUNTS_PATH, CONFIG_PATH, GITHUB_PAT, GITHUB_REPO

app = Flask(__name__)

# Add CORS headers to all responses
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# Country flags
COUNTRY_FLAGS = {
    "US": "🇺🇸",
    "NL": "🇳🇱",
    "GB": "🇬🇧",
    "JP": "🇯🇵",
    "DE": "🇩🇪",
    "ES": "🇪🇸",
    "PL": "🇵🇱"
}

def get_country_flag(country):
    return COUNTRY_FLAGS.get(country, "🇺🇸")

def load_matrix_accounts():
    # 1. Fetch live persistent tokens from GitHub vault branch
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/accounts.json?ref=vault"
        headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json"
        }
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            content_b64 = r.json().get("content", "")
            raw = base64.b64decode(content_b64).decode("utf-8")
            accs = json.loads(raw)
            if accs:
                try:
                    with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
                        f.write(raw)
                except Exception:
                    pass
                print(f"[OK] Successfully loaded {len(accs)} accounts from Persistent GitHub Cloud Vault (vault branch).")
                return accs
    except Exception as e:
        print(f"[!] Warning: Could not fetch from cloud vault: {e}")

    # 2. Fallback to local accounts.json
    if os.path.exists(ACCOUNTS_PATH):
        try:
            with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error loading accounts.json: {e}")
    return []

def load_app_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_daily_account_cap(account_id, config=None):
    if not config:
        config = load_app_config()
    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    seed = f"{today_str}_{account_id}"
    h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
    cap_min = float(config.get("daily_cap_min", 30.0))
    cap_max = float(config.get("daily_cap_max", 35.0))
    ratio = (h % 1000) / 1000.0
    return round(cap_min + ratio * (cap_max - cap_min), 2)

# Matrix global state
matrix_state = {
    "summary": {
        "total_zaps": 0.0,
        "daily_zaps": 0.0,
        "active_nodes": 0,
        "total_nodes": 5,
        "target_daily": 160.0
    },
    "accounts": {},
    "history": [],
    "last_updated": int(time.time())
}

matrix_lock = threading.Lock()
running_workers = {}

def account_worker(acc_data, initial_delay=0):
    global matrix_state
    acc_id = acc_data["id"]
    name = acc_data.get("name", acc_id)
    country = acc_data.get("country", "US")
    flag = get_country_flag(country)

    if initial_delay > 0:
        time.sleep(initial_delay)

    client = ConsoClient(account_data=acc_data)

    while True:
        try:
            # Check if account has credentials injected
            if not client.refresh_token and not client.access_token:
                with matrix_lock:
                    if acc_id in matrix_state["accounts"]:
                        matrix_state["accounts"][acc_id]["status"] = "waiting_for_session"
                time.sleep(10)
                # Re-check updated file/state
                with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                    updated_accs = json.load(f)
                for a in updated_accs:
                    if a.get("id") == acc_id:
                        if a.get("refresh_token"):
                            client.refresh_token = a.get("refresh_token")
                            client.access_token = a.get("access_token")
                            client.email = a.get("email")
                            client.account_name = a.get("name")
                            break
                continue

            # Authenticate & fetch profile
            p = None
            try:
                p = client.get_profile()
                if not p:
                    client.refresh_session()
                    p = client.get_profile()
                status_msg = "active"
            except Exception as e:
                status_msg = f"auth_err: {str(e)[:30]}"
                print(f"[!] Worker Auth Error for {name}: {e}")

            rank = client.get_my_rank()

            with matrix_lock:
                if acc_id in matrix_state["accounts"]:
                    acc_s = matrix_state["accounts"][acc_id]
                    acc_s["status"] = status_msg
                    if p:
                        acc_s["total_zaps"] = float(p.get("total_zaps", 0) or 0.0)
                        today_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                        if p.get("daily_zaps_date") == today_utc:
                            acc_s["daily_zaps"] = float(p.get("daily_zaps_earned", 0) or 0.0)
                        else:
                            acc_s["daily_zaps"] = 0.0
                        acc_s["streak"] = p.get("current_streak", 0) or 1
                        acc_s["boost"] = p.get("boost_factor", 1.05) or 1.05
                        acc_s["email"] = p.get("email", client.email)
                    if rank:
                        acc_s["rank"] = rank

            # Auto-claim daily check-in (+2 Zaps)
            try:
                if client.config.get("auto_claim_daily_checkin", True):
                    daily_claims = client.get_todays_missions()
                    if "daily-checkin-v1" not in daily_claims:
                        ok, _ = client.claim_daily_mission("daily-checkin-v1")
                        if ok:
                            print(f"[✓] {flag} {name}: Auto-claimed daily check-in (+2 Zaps)")
            except Exception:
                pass

            daily_cap = get_daily_account_cap(acc_id, client.config)
            with matrix_lock:
                acc_s["daily_cap"] = daily_cap

            # Daily Cap Safety Check
            if acc_s.get("daily_zaps", 0.0) >= daily_cap:
                with matrix_lock:
                    acc_s["status"] = "daily_cap_reached"
                    acc_s["is_sleeping"] = False
                    acc_s["next_sync_target"] = int(time.time() + 900)
                time.sleep(900)
                continue

            # Sleep window check
            if client.is_in_sleep_window():
                with matrix_lock:
                    acc_s["status"] = "human_sleep_mode"
                    acc_s["is_sleeping"] = True
                    acc_s["next_sync_target"] = int(time.time() + 1200)
                time.sleep(1200)
                continue

            with matrix_lock:
                acc_s["is_sleeping"] = False
                acc_s["status"] = "active"

            # Simulate turn on rotating platform
            preset = random.choice(PLATFORM_PRESETS)
            result = client.simulate_prompt(preset)

            if result["ok"]:
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                credited = result["credited_zaps"]

                with matrix_lock:
                    acc_s["last_sync"] = now_str
                    acc_s["last_platform"] = preset["platform"].capitalize()
                    acc_s["last_credited"] = credited

                    history_entry = {
                        "time": now_str,
                        "account": f"{flag} {name}",
                        "name": name,
                        "country": country,
                        "platform": preset["platform"].capitalize(),
                        "model": preset["model"],
                        "tokens": result["tokens"],
                        "zaps": f"+{credited:.2f}"
                    }
                    matrix_state["history"].insert(0, history_entry)
                    if len(matrix_state["history"]) > 60:
                        matrix_state["history"].pop()

                print(f"[+] [{now_str}] {flag} {name}: +{credited:.2f} Zaps ({preset['platform'].capitalize()})")

            # Humanized delay between turns (18 - 30 minutes)
            min_sec = client.config.get("min_interval_sec", 1100)
            max_sec = client.config.get("max_interval_sec", 1800)
            sleep_duration = random.randint(min_sec, max_sec)

            with matrix_lock:
                acc_s["next_sync_target"] = int(time.time() + sleep_duration)

            time.sleep(sleep_duration)

        except Exception as e:
            print(f"[!] Error in worker {acc_id}: {e}")
            time.sleep(60)

def start_matrix():
    accounts = load_matrix_accounts()
    print(f"[*] Starting Conso Zap Cluster 2 with {len(accounts)} slots...")

    with matrix_lock:
        matrix_state["summary"]["total_nodes"] = len(accounts)
        for acc in accounts:
            acc_id = acc["id"]
            country = acc.get("country", "US")
            proxy_str = acc.get("proxy", "")
            assigned_ip = proxy_str.split("@")[-1] if "@" in proxy_str else (proxy_str or "Cloud Direct")

            matrix_state["accounts"][acc_id] = {
                "id": acc_id,
                "name": acc.get("name", acc_id),
                "email": acc.get("email", ""),
                "country": country,
                "flag": get_country_flag(country),
                "proxy": assigned_ip,
                "status": "active" if acc.get("refresh_token") else "waiting_for_session",
                "total_zaps": 0.0,
                "daily_zaps": 0.0,
                "streak": 1,
                "boost": 1.05,
                "rank": None,
                "last_sync": "--:--:--",
                "last_platform": "--",
                "last_credited": 0.0,
                "is_sleeping": False,
                "next_sync_target": 0
            }

    # Staggered launch across slots
    stagger = 0
    for acc in accounts:
        acc_id = acc["id"]
        t = threading.Thread(target=account_worker, args=(acc, stagger), daemon=True)
        running_workers[acc_id] = t
        t.start()
        stagger += 12

threading.Thread(target=start_matrix, daemon=True).start()

# ----------------- FLASK ROUTES -----------------

@app.route("/")
def dashboard():
    with matrix_lock:
        state_copy = json.loads(json.dumps(matrix_state))
    
    total_zaps = sum(a.get("total_zaps", 0) for a in state_copy["accounts"].values())
    daily_zaps = sum(a.get("daily_zaps", 0) for a in state_copy["accounts"].values())
    active_cnt = sum(1 for a in state_copy["accounts"].values() if a.get("status") in ("active", "daily_cap_reached"))

    state_copy["summary"]["total_zaps"] = round(total_zaps, 2)
    state_copy["summary"]["daily_zaps"] = round(daily_zaps, 2)
    state_copy["summary"]["active_nodes"] = active_cnt

    return render_template_string(HTML_TEMPLATE, state=state_copy)

@app.route("/health")
def health():
    with matrix_lock:
        state_copy = json.loads(json.dumps(matrix_state))
    return jsonify({
        "status": "ok",
        "service": "conso-zap-cluster2",
        "cluster": "Cluster 2 (Oxylabs 5 Dedicated Slots)",
        "active_nodes": sum(1 for a in state_copy["accounts"].values() if a.get("status") in ("active", "daily_cap_reached")),
        "total_nodes": len(state_copy["accounts"]),
        "accounts": state_copy["accounts"]
    }), 200

@app.route("/api/status")
def api_status():
    with matrix_lock:
        state_copy = json.loads(json.dumps(matrix_state))
    return jsonify(state_copy), 200

@app.route("/api/add_account", methods=["POST", "OPTIONS"])
def api_add_account():
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200

    data = request.json or {}
    email = data.get("email", "").strip()
    refresh_token = data.get("refresh_token", "").strip()
    access_token = data.get("access_token", "").strip()
    name = data.get("name", "").strip()
    slot_id = data.get("slot_id", "").strip()

    if not refresh_token:
        return jsonify({"ok": False, "error": "refresh_token is required"}), 400

    # Load current accounts
    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    target_slot = None
    if slot_id:
        for acc in accounts:
            if acc["id"] == slot_id:
                target_slot = acc
                break
    
    # Auto-assign to first empty or matching slot
    if not target_slot:
        for acc in accounts:
            if acc.get("email") == email or not acc.get("refresh_token"):
                target_slot = acc
                break

    if not target_slot:
        return jsonify({"ok": False, "error": "All 5 Oxylabs slots are currently occupied!"}), 400

    # Inject credentials
    target_slot["refresh_token"] = refresh_token
    if access_token:
        target_slot["access_token"] = access_token
    if email:
        target_slot["email"] = email
    if name:
        target_slot["name"] = name
    else:
        target_slot["name"] = email.split("@")[0] if email else target_slot["name"]

    # Save to disk
    with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

    # Trigger force sync to GitHub vault branch
    c = ConsoClient(account_data=target_slot)
    c.refresh_token = refresh_token
    c.access_token = access_token
    c.save_config(force=True)

    # Update in-memory state
    with matrix_lock:
        acc_s = matrix_state["accounts"].get(target_slot["id"])
        if acc_s:
            acc_s["email"] = target_slot["email"]
            acc_s["name"] = target_slot["name"]
            acc_s["status"] = "active"

    return jsonify({
        "ok": True,
        "message": f"Successfully synced {target_slot['name']} to Slot {target_slot['id']}!",
        "slot": target_slot["id"],
        "proxy": target_slot.get("proxy", "")
    }), 200

# ----------------- CYBERPUNK UI TEMPLATE -----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>⚡ CONSO ZAP CLUSTER 2 - Oxylabs Farm</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090b10;
      --card-bg: rgba(16, 21, 33, 0.85);
      --border: rgba(56, 189, 248, 0.2);
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.35);
      --gold: #fbbf24;
      --green: #10b981;
      --red: #ef4444;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
    }
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Space Grotesk', sans-serif;
      padding: 24px;
      min-height: 100vh;
      background-image: radial-gradient(circle at 50% 0%, rgba(56, 189, 248, 0.08), transparent 50%);
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .title-group h1 {
      font-size: 26px;
      font-weight: 800;
      background: linear-gradient(135deg, #38bdf8, #818cf8);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .badge-cluster {
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 20px;
      border: 1px solid rgba(56, 189, 248, 0.3);
      font-family: 'JetBrains Mono', monospace;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      backdrop-filter: blur(8px);
    }
    .stat-title {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .stat-val {
      font-size: 28px;
      font-weight: 800;
      color: #fff;
      font-family: 'JetBrains Mono', monospace;
    }
    .grid-nodes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
      margin-bottom: 28px;
    }
    .node-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      transition: all 0.2s ease;
    }
    .node-card:hover {
      border-color: var(--accent);
      box-shadow: 0 4px 20px var(--accent-glow);
    }
    .node-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .node-title {
      font-size: 16px;
      font-weight: 700;
    }
    .node-status {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 12px;
      font-family: 'JetBrains Mono', monospace;
      font-weight: 600;
    }
    .status-active { background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid #10b981; }
    .status-cap { background: rgba(251, 191, 36, 0.2); color: #fbbf24; border: 1px solid #fbbf24; }
    .status-waiting { background: rgba(148, 163, 184, 0.2); color: #94a3b8; border: 1px solid #94a3b8; }
    .data-row {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      margin-bottom: 8px;
    }
    .data-label { color: var(--text-muted); }
    .data-val { font-family: 'JetBrains Mono', monospace; font-weight: 600; }
    .terminal-section {
      background: #05070a;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }
    .terminal-header {
      font-size: 13px;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .terminal-body {
      max-height: 220px;
      overflow-y: auto;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
    }
    .log-line {
      margin-bottom: 6px;
      color: #94a3b8;
    }
    .log-time { color: #64748b; }
    .log-zap { color: #10b981; font-weight: 700; }
  </style>
</head>
<body>

  <div class="header">
    <div class="title-group">
      <h1><i class="fa-solid fa-bolt"></i> CONSO ZAP CLUSTER 2 <span class="badge-cluster">Oxylabs 5 Slots</span></h1>
    </div>
    <button onclick="location.reload()" style="background:transparent; border:1px solid var(--border); color:#fff; padding:8px 16px; border-radius:8px; cursor:pointer; font-family:'JetBrains Mono';"><i class="fa-solid fa-rotate-right"></i> Refresh</button>
  </div>

  <div class="summary-grid">
    <div class="stat-card">
      <div class="stat-title">Total Zaps Mined</div>
      <div class="stat-val" style="color:#38bdf8;">{{ state.summary.total_zaps }} ⚡</div>
    </div>
    <div class="stat-card">
      <div class="stat-title">Daily Zaps (Today)</div>
      <div class="stat-val" style="color:#10b981;">{{ state.summary.daily_zaps }} ⚡</div>
    </div>
    <div class="stat-card">
      <div class="stat-title">Active Oxylabs Nodes</div>
      <div class="stat-val" style="color:#fbbf24;">{{ state.summary.active_nodes }} / 5</div>
    </div>
  </div>

  <h2 style="font-size: 16px; margin-bottom: 14px; color: var(--accent);"><i class="fa-solid fa-server"></i> Dedicated Oxylabs Proxy Slots</h2>
  <div class="grid-nodes">
    {% for acc_id, a in state.accounts.items() %}
    <div class="node-card">
      <div class="node-header">
        <div class="node-title">{{ a.flag }} {{ a.name }}</div>
        <div class="node-status {% if a.status == 'active' %}status-active{% elif a.status == 'daily_cap_reached' %}status-cap{% else %}status-waiting{% endif %}">
          {{ a.status }}
        </div>
      </div>
      <div class="data-row">
        <span class="data-label">Email:</span>
        <span class="data-val">{{ a.email or 'Pending Grabber Sync' }}</span>
      </div>
      <div class="data-row">
        <span class="data-label">Oxylabs Proxy:</span>
        <span class="data-val" style="color:#38bdf8;">{{ a.proxy }}</span>
      </div>
      <div class="data-row">
        <span class="data-label">Daily Zaps:</span>
        <span class="data-val" style="color:#10b981;">{{ a.daily_zaps }} / {{ a.daily_cap or '35.0' }} ⚡</span>
      </div>
      <div class="data-row">
        <span class="data-label">Total Zaps:</span>
        <span class="data-val">{{ a.total_zaps }} ⚡</span>
      </div>
      <div class="data-row">
        <span class="data-label">Global Rank:</span>
        <span class="data-val" style="color:#fbbf24;">#{{ a.rank or 'Unranked' }}</span>
      </div>
      <div class="data-row">
        <span class="data-label">Streak / Boost:</span>
        <span class="data-val">{{ a.streak }}d ({{ a.boost }}x)</span>
      </div>
      <div class="data-row">
        <span class="data-label">Last Synced:</span>
        <span class="data-val">{{ a.last_sync }} ({{ a.last_platform }})</span>
      </div>
    </div>
    {% endfor %}
  </div>

  <div class="terminal-section">
    <div class="terminal-header"><i class="fa-solid fa-terminal"></i> Live Cluster Activity Log</div>
    <div class="terminal-body">
      {% for h in state.history %}
      <div class="log-line">
        <span class="log-time">[{{ h.time }}]</span> {{ h.account }} &bull; {{ h.platform }} ({{ h.model }}) &bull; {{ h.tokens }} tok &bull; <span class="log-zap">{{ h.zaps }} Zaps</span>
      </div>
      {% endfor %}
      {% if not state.history %}
      <div class="log-line" style="color:#64748b;">No prompt turns recorded yet. Waiting for first session synchronization...</div>
      {% endif %}
    </div>
  </div>

</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
