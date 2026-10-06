import os
import json
import time
import random
import datetime
import requests
import base64
import threading

cloud_vault_lock = threading.Lock()
LAST_VAULT_SYNC = 0

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "accounts.json")

# Verified Models & Multipliers matching Conso extension source code
PLATFORM_PRESETS = [
    {
        "platform": "claude",
        "model": "claude-3-5-sonnet",
        "weight": 0.7,
        "input_cost_per_m": 3.0,
        "output_cost_per_m": 15.0
    },
    {
        "platform": "chatgpt",
        "model": "gpt-4o",
        "weight": 0.3,
        "input_cost_per_m": 2.5,
        "output_cost_per_m": 10.0
    },
    {
        "platform": "perplexity",
        "model": "pplx_asi_sonnet",
        "weight": 0.7,
        "input_cost_per_m": 3.0,
        "output_cost_per_m": 15.0
    },
    {
        "platform": "gemini",
        "model": "gemini-1.5-pro",
        "weight": 0.25,
        "input_cost_per_m": 1.25,
        "output_cost_per_m": 5.0
    }
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Origin": "chrome-extension://bjibbmkefnaamkenamdppfengeepadpi",
    "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9"
}

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "ABMDrop/conso-zap2")

class ConsoClient:
    def __init__(self, config_path=CONFIG_PATH, account_data=None):
        self.config_path = config_path
        self.load_config()
        self.account_data = account_data or {}
        
        self.account_id = self.account_data.get("id", "acc_01")
        self.account_name = self.account_data.get("name", "Cluster 2 Node")
        self.email = self.account_data.get("email", "")
        self.country = self.account_data.get("country", "US")
        self.proxy = self.account_data.get("proxy", None)
        self.refresh_token = self.account_data.get("refresh_token", "")
        self.access_token = self.account_data.get("access_token", None)

        self.session = requests.Session()
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

        self.user_profile = None

    def load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = {}
        
        self.supabase_url = os.environ.get("CONSO_SUPABASE_URL", self.config.get("supabase_url", "https://jzxlayjrsdbyzykuiqns.supabase.co"))
        self.anon_key = os.environ.get("CONSO_ANON_KEY", self.config.get("anon_key", "sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd"))

    def save_config(self, force=False):
        if os.path.exists(ACCOUNTS_PATH) and hasattr(self, "account_id") and self.account_id:
            try:
                with cloud_vault_lock:
                    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                        accs = json.load(f)
                    for a in accs:
                        if a.get("id") == self.account_id:
                            a["refresh_token"] = self.refresh_token
                            if self.access_token:
                                a["access_token"] = self.access_token
                            if self.email:
                                a["email"] = self.email
                            if self.account_name:
                                a["name"] = self.account_name
                            break
                    with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
                        json.dump(accs, f, indent=2)
            except Exception:
                pass
            
            # Asynchronously sync to persistent GitHub vault branch with 30m debounce
            threading.Thread(target=self.sync_to_cloud_vault, args=(force,), daemon=True).start()

    def sync_to_cloud_vault(self, force=False):
        global LAST_VAULT_SYNC
        now = time.time()
        if not force and (now - LAST_VAULT_SYNC) < 1800:
            return

        with cloud_vault_lock:
            if not force and (time.time() - LAST_VAULT_SYNC) < 1800:
                return
            LAST_VAULT_SYNC = time.time()

            for attempt in range(3):
                try:
                    if not os.path.exists(ACCOUNTS_PATH):
                        return
                    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                        local_content = f.read()

                    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/accounts.json"
                    headers = {
                        "Authorization": f"token {GITHUB_PAT}",
                        "Accept": "application/vnd.github.v3+json"
                    }
                    r_get = requests.get(f"{url}?ref=vault", headers=headers, timeout=10)
                    sha = None
                    if r_get.status_code == 200:
                        sha = r_get.json().get("sha")

                    body = {
                        "message": f"Vault auto-sync: Cluster 2 state [{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]",
                        "content": base64.b64encode(local_content.encode("utf-8")).decode("utf-8"),
                        "branch": "vault"
                    }
                    if sha:
                        body["sha"] = sha

                    r_put = requests.put(url, headers=headers, json=body, timeout=15)
                    if r_put.status_code in (200, 201):
                        print(f"[✓] Cloud Vault updated on branch 'vault'")
                        return
                    elif r_put.status_code == 409:
                        time.sleep(random.uniform(1.0, 2.0))
                        continue
                except Exception as e:
                    print(f"[!] Vault sync notice: {e}")
                    time.sleep(1)

    def refresh_session(self):
        if not self.refresh_token:
            return False

        url = f"{self.supabase_url}/auth/v1/token?grant_type=refresh_token"
        headers = dict(BROWSER_HEADERS)
        headers.update({
            "apikey": self.anon_key,
            "Content-Type": "application/json"
        })
        res = self.session.post(url, headers=headers, json={"refresh_token": self.refresh_token}, timeout=15)
        if res.status_code == 200:
            data = res.json()
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self.token_expiry = time.time() + 3000  # 50 minutes cache
            self.save_config()
            return True
        else:
            self.token_expiry = 0
            raise Exception(f"Session Refresh Failed ({res.status_code}): {res.text[:80]}")

    def get_auth_headers(self):
        now = time.time()
        if not self.access_token or now >= getattr(self, "token_expiry", 0):
            self.refresh_session()
        headers = dict(BROWSER_HEADERS)
        headers.update({
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        })
        return headers

    def api_request(self, method, endpoint, payload=None, retry_auth=True):
        url = f"{self.supabase_url}/rest/v1/{endpoint}"
        headers = self.get_auth_headers()
        try:
            if method.upper() == "GET":
                res = self.session.get(url, headers=headers, timeout=15)
            else:
                res = self.session.post(url, headers=headers, json=payload or {}, timeout=15)
            
            if res.status_code == 401 and retry_auth:
                self.token_expiry = 0
                self.refresh_session()
                headers = self.get_auth_headers()
                if method.upper() == "GET":
                    res = self.session.get(url, headers=headers, timeout=15)
                else:
                    res = self.session.post(url, headers=headers, json=payload or {}, timeout=15)
            return res
        except Exception as e:
            raise e

    def get_profile(self):
        if not self.access_token and not self.refresh_token:
            return None
        res = self.api_request("GET", "consousers?select=*")
        if res.status_code == 200:
            rows = res.json()
            if rows:
                self.user_profile = rows[0]
                return self.user_profile
        return None

    def get_my_rank(self):
        if not self.access_token and not self.refresh_token:
            return None
        res = self.api_request("POST", "rpc/get_my_leaderboard_rank", {})
        if res.status_code == 200:
            return res.json()
        return None

    def get_top_leaderboard(self, limit=10):
        res = self.api_request("GET", f"leaderboard_top?select=*&order=total_zaps.desc&limit={limit}")
        if res.status_code == 200:
            return res.json()
        return []

    def get_todays_missions(self):
        if not self.access_token and not self.refresh_token:
            return []
        res = self.api_request("POST", "rpc/get_todays_mission_claims", {})
        if res.status_code == 200:
            return res.json()
        return []

    def claim_daily_mission(self, mission_id="daily-checkin-v1", claim_ref=None):
        payload = {
            "p_mission_id": mission_id,
            "p_claim_ref": claim_ref
        }
        res = self.api_request("POST", "rpc/claim_daily_mission", payload)
        return res.status_code == 200, res.text

    def is_in_sleep_window(self):
        if not self.config.get("enable_sleep_window", False):
            return False
        now_utc = datetime.datetime.now(datetime.timezone.utc).hour
        sleep_start = self.config.get("sleep_start_hour_utc", 21)
        sleep_end = self.config.get("sleep_end_hour_utc", 3)
        if sleep_start <= sleep_end:
            return sleep_start <= now_utc < sleep_end
        else:
            return now_utc >= sleep_start or now_utc < sleep_end

    def simulate_prompt(self, preset=None):
        if not preset:
            preset = random.choice(PLATFORM_PRESETS)
        
        input_tokens = random.randint(140, 260)
        output_tokens = random.randint(380, 680)
        total_tokens = input_tokens + output_tokens
        prompt_quality = round(random.uniform(4.0, 4.9), 1)

        base_zaps = round((total_tokens / 10000.0) * prompt_quality * preset["weight"] * 2.5, 2)
        
        spend_usd = round(
            (input_tokens / 1e6 * preset["input_cost_per_m"]) + 
            (output_tokens / 1e6 * preset["output_cost_per_m"]),
            6
        )

        p_entry = {
            "platform": preset["platform"],
            "model": preset["model"],
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "inputFilesCount": 0,
            "outputFilesCount": 0,
            "promptQuality": prompt_quality
        }

        payload = {
            "p_entry": p_entry,
            "p_base_zaps": base_zaps,
            "p_spend_usd": spend_usd
        }

        res = self.api_request("POST", "rpc/append_prompt", payload)
        if res.status_code == 200:
            credited = float(res.text) if res.text.replace(".", "", 1).isdigit() else base_zaps
            return {
                "ok": True,
                "platform": preset["platform"],
                "model": preset["model"],
                "tokens": total_tokens,
                "quality": prompt_quality,
                "base_zaps": base_zaps,
                "credited_zaps": credited
            }
        else:
            return {
                "ok": False,
                "error": res.text,
                "status": res.status_code
            }
