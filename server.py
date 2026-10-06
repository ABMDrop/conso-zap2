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
    if GITHUB_PAT:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/accounts.json?ref=vault"
            headers = {
                "Authorization": f"token {GITHUB_PAT}",
                "Accept": "application/vnd.github.v3+json"
            }
            r = requests.get(url, headers=headers, timeout=5)
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
    stagger = 15
    for acc in accounts:
        acc_id = acc["id"]
        matrix_state["accounts"][acc_id]["next_sync_target"] = int(time.time() + stagger)
        t = threading.Thread(target=account_worker, args=(acc, stagger), daemon=True)
        running_workers[acc_id] = t
        t.start()
        stagger += 35

MATRIX_INITIALIZED = False
MATRIX_INIT_LOCK = threading.Lock()

def ensure_matrix_started():
    global MATRIX_INITIALIZED
    if MATRIX_INITIALIZED:
        return
    with MATRIX_INIT_LOCK:
        if MATRIX_INITIALIZED:
            return
        start_matrix()
        MATRIX_INITIALIZED = True

ensure_matrix_started()

# ----------------- FLASK ROUTES -----------------

@app.route("/")
def dashboard():
    ensure_matrix_started()
    return render_template_string(HTML_TEMPLATE)

@app.route("/health")
def health():
    ensure_matrix_started()
    with matrix_lock:
        tot = sum(float(a.get("total_zaps", 0)) for a in matrix_state["accounts"].values())
        day = sum(float(a.get("daily_zaps", 0)) for a in matrix_state["accounts"].values())
        active_cnt = sum(1 for a in matrix_state["accounts"].values() if a.get("status") in ("active", "daily_cap_reached"))
        total_accs = len(matrix_state["accounts"])
        matrix_state["summary"]["total_zaps"] = round(tot, 2)
        matrix_state["summary"]["daily_zaps"] = round(day, 2)
        matrix_state["summary"]["active_nodes"] = active_cnt
        matrix_state["summary"]["total_nodes"] = total_accs
        target_sum = sum(float(a.get("daily_cap", 32.5)) for a in matrix_state["accounts"].values())
        matrix_state["summary"]["target_daily"] = round(target_sum, 1)

        state_copy = json.loads(json.dumps(matrix_state))
        legacy_state = state_copy["accounts"].get("acc_01", {})

        return jsonify({
            "ok": True,
            "status": "ok",
            "service": "conso-zap-cluster2",
            "cluster": "Cluster 2 (Oxylabs 5 Dedicated Slots)",
            "matrix": state_copy,
            "state": legacy_state,
            "accounts": state_copy["accounts"],
            "active_nodes": active_cnt,
            "total_nodes": total_accs
        }), 200

@app.route("/api/status")
def api_status():
    ensure_matrix_started()
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

    ensure_matrix_started()

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title id="page_title">Conso 5-Account Golden Matrix Hub (Cluster 2)</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #090d16; color: #f8fafc; margin: 0; padding: 24px; }
        .container { max-width: 1100px; margin: 0 auto; }
        .card { background: #131b2e; border-radius: 16px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid #1e293b; margin-bottom: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 20px; }
        h1 { margin: 0; font-size: 22px; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
        .badge-active { background: #064e3b; color: #34d399; }
        .badge-sleep { background: #1e1b4b; color: #a5b4fc; }
        .badge-cap { background: #713f12; color: #fde047; }
        .badge-err { background: #7f1d1d; color: #fca5a5; }
        .badge-queue { background: #1e293b; color: #94a3b8; }
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
        .stat-box { background: #090d16; border-radius: 12px; padding: 16px; border: 1px solid #1e293b; }
        .stat-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; margin-bottom: 6px; letter-spacing: 0.5px; }
        .stat-val { font-size: 24px; font-weight: 800; color: #f8fafc; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
        th { text-align: left; padding: 12px 10px; color: #94a3b8; border-bottom: 1px solid #1e293b; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
        td { padding: 14px 10px; border-bottom: 1px solid #131b2e; color: #cbd5e1; }
        tr:hover td { background: #17223b; }
        .countdown { font-family: monospace; font-size: 14px; font-weight: 800; color: #38bdf8; background: #090d16; padding: 4px 8px; border-radius: 6px; border: 1px solid #1e293b; }
        .progress-bar-bg { width: 100px; height: 6px; background: #1e293b; border-radius: 3px; overflow: hidden; margin-top: 4px; }
        .progress-bar-fill { height: 100%; background: #38bdf8; border-radius: 3px; }
        .footer { font-size: 12px; color: #64748b; margin-top: 24px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <!-- Top Command Header -->
        <div class="card">
            <div class="header">
                <h1 id="header_title">⚡ CONSO 5-ACCOUNT GOLDEN MATRIX COMMAND CENTER</h1>
                <span class="badge badge-active" id="header_badge">5 OXYLABS SLOTS ONLINE • 24/7 CLOUD</span>
            </div>

            <!-- Top Summary Stats -->
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-label">Total Matrix Zaps</div>
                    <div class="stat-val" style="color: #38bdf8;" id="total_matrix_zaps">Calculating...</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Today Matrix Progress</div>
                    <div class="stat-val" style="color: #4ade80;" id="daily_matrix_zaps">Calculating...</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Active Nodes</div>
                    <div class="stat-val" id="active_nodes">Connecting...</div>
                </div>
                <div class="stat-box">
                    <div class="stat-label">Daily Safety Cap</div>
                    <div class="stat-val" style="color: #fde047;" id="daily_safety_cap">Calculating...</div>
                </div>
            </div>

            <!-- Multi-Account Matrix Roster Table -->
            <h3 style="margin: 20px 0 10px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">
                💎 Multi-Account Node Roster (Oxylabs Dedicated IP Isolation)
            </h3>
            <table>
                <thead>
                    <tr>
                        <th>Account</th>
                        <th>Proxy / Location</th>
                        <th>Total Zaps</th>
                        <th>Today's Progress</th>
                        <th>Rank / Streak</th>
                        <th>Next Prompt In</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="matrix-body">
                    <tr><td colspan="7" style="text-align: center; padding: 20px; color: #64748b;">Loading Matrix Nodes...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Global Activity Log -->
        <div class="card">
            <h3 style="margin: 0 0 14px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;">
                📜 Real-Time Multi-Node Activity Log
            </h3>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Node</th>
                        <th>Platform</th>
                        <th>Model</th>
                        <th>Tokens</th>
                        <th>Zaps Earned</th>
                    </tr>
                </thead>
                <tbody id="history-body">
                    <tr><td colspan="6" style="text-align: center; padding: 20px; color: #64748b;">Waiting for turns to log...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="footer">
            Dedicated Oxylabs Geo Routing: 🇺🇸 San Francisco / California (Ports 8001 - 8005) | 100% Anti-Sybil Isolation & WebRTC Leak Shield
        </div>
    </div>

    <script>
        let accountsData = {};

        function getFlagHtml(country) {
            if (!country || country === "??") return "🌐";
            let c = country.toLowerCase();
            return `<img src="https://flagcdn.com/20x15/${c}.png" width="20" height="15" alt="${country}" style="border-radius: 2px; vertical-align: middle; margin-right: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.4);" onerror="this.outerHTML='🌐'">`;
        }

        function formatCountdown(targetEpoch) {
            let now = Math.floor(Date.now() / 1000);
            let diff = targetEpoch - now;
            if (diff <= 0) return "⚡ Syncing...";
            let m = Math.floor(diff / 60);
            let s = diff % 60;
            return (m < 10 ? "0" : "") + m + "m " + (s < 10 ? "0" : "") + s + "s";
        }

        function updateTickers() {
            for (let aid in accountsData) {
                let el = document.getElementById("timer-" + aid);
                if (el && accountsData[aid].next_sync_target) {
                    el.innerText = formatCountdown(accountsData[aid].next_sync_target);
                }
            }
        }

        async function fetchMatrixState() {
            try {
                let res = await fetch("/health");
                let data = await res.json();
                if (data.ok && data.matrix) {
                    let m = data.matrix;
                    accountsData = m.accounts;

                    let totalNodes = Object.keys(m.accounts).length;
                    let totalTargetCap = 0;
                    for (let aid in m.accounts) {
                        totalTargetCap += Number(m.accounts[aid].daily_cap || 33.0);
                    }
                    let targetCap = totalTargetCap.toFixed(1);

                    // Dynamic Titles & Badges
                    document.title = `Conso ${totalNodes}-Account Golden Matrix Hub (Cluster 2)`;
                    let headerTitleEl = document.getElementById("header_title");
                    if (headerTitleEl) headerTitleEl.innerHTML = `⚡ CONSO ${totalNodes}-ACCOUNT GOLDEN MATRIX COMMAND CENTER`;
                    let headerBadgeEl = document.getElementById("header_badge");
                    if (headerBadgeEl) headerBadgeEl.innerText = `${totalNodes} OXYLABS SLOTS ONLINE • 24/7 CLOUD`;

                    // Update Top Stats
                    let totalZ = 0;
                    let dailyZ = 0;
                    let activeCount = 0;

                    let tbody = document.getElementById("matrix-body");
                    let rowsHtml = "";

                    for (let aid in m.accounts) {
                        let acc = m.accounts[aid];
                        totalZ += Number(acc.total_zaps || 0);
                        dailyZ += Number(acc.daily_zaps || 0);
                        if (acc.status === "active") activeCount++;

                        let cap = Number(acc.daily_cap || 33.0);
                        let pct = Math.min(100, Math.round(((acc.daily_zaps || 0) / cap) * 100));
                        let badgeClass = acc.is_sleeping ? "badge-sleep" : (acc.status === "daily_cap_reached" ? "badge-cap" : (acc.status.includes("err") ? "badge-err" : (acc.status === "staggered_queue" ? "badge-queue" : "badge-active")));
                        let flag = getFlagHtml(acc.country || "US");

                        rowsHtml += `
                            <tr>
                                <td><b>${flag} ${acc.name}</b><br><span style="font-size: 11px; color: #64748b;">${acc.email || ''}</span></td>
                                <td style="font-size: 12px; color: #94a3b8;">${acc.country || 'US'} • ${acc.proxy.includes("@") ? acc.proxy.split("@")[1] : acc.proxy}</td>
                                <td style="font-weight: bold; color: #38bdf8;">${Number(acc.total_zaps || 0).toFixed(2)}</td>
                                <td>
                                    <div>${Number(acc.daily_zaps || 0).toFixed(2)} / ${cap.toFixed(2)}</div>
                                    <div class="progress-bar-bg"><div class="progress-bar-fill" style="width: ${pct}%;"></div></div>
                                </td>
                                <td>#${acc.rank || "..."} <span style="font-size: 11px; color: #94a3b8;">(${acc.streak || 1}d)</span></td>
                                <td><span class="countdown" id="timer-${acc.id}">${formatCountdown(acc.next_sync_target || 0)}</span></td>
                                <td><span class="badge ${badgeClass}">${acc.status}</span></td>
                            </tr>
                        `;
                    }

                    tbody.innerHTML = rowsHtml;
                    document.getElementById("total_matrix_zaps").innerText = totalZ.toFixed(2);
                    document.getElementById("daily_matrix_zaps").innerText = `${dailyZ.toFixed(2)} / ${targetCap}`;
                    document.getElementById("active_nodes").innerText = `${activeCount} / ${totalNodes} Online`;
                    let dailySafetyCapEl = document.getElementById("daily_safety_cap");
                    if (dailySafetyCapEl) dailySafetyCapEl.innerText = `${targetCap} Zaps/Day`;

                    // Update History Table
                    if (m.history && m.history.length > 0) {
                        let hbody = document.getElementById("history-body");
                        hbody.innerHTML = m.history.map(h => {
                            let hFlag = getFlagHtml(h.country || "US");
                            let nodeTitle = h.name ? `${hFlag} ${h.name}` : (h.country ? `${hFlag} ${h.account.replace(/^[^ ]+ /, '')}` : (h.account || "Node"));
                            return `
                            <tr>
                                <td>${h.time}</td>
                                <td><b>${nodeTitle}</b></td>
                                <td>${h.platform}</td>
                                <td>${h.model}</td>
                                <td>${h.tokens}</td>
                                <td style="color: #4ade80; font-weight: bold;">${h.zaps}</td>
                            </tr>
                        `;
                        }).join("");
                    }
                }
            } catch (e) {
                console.log("Fetch error:", e);
            }
        }

        setInterval(updateTickers, 1000);
        setInterval(fetchMatrixState, 4000);
        fetchMatrixState();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
