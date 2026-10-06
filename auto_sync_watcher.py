import os
import re
import json
import time
import requests

# ----------------- IRON-CLAD CLUSTER 1 PROTECTIONS -----------------
# 1. Directory-level exclusion: The script will NEVER open or read these folders
EXCLUDED_PROFILES = {
    "Default",
    "Profile 2",
    "Profile 19",
    "Profile 23",
    "Profile 38",
    "Profile 39",
    "Profile 40",
    "Profile 44"
}

# 2. Email blacklist: Cluster 1 accounts are 100% quarantined
CLUSTER_1_BLACKLIST_EMAILS = {
    "cryptoofi@gmail.com",
    "meniyakhushiak@gmail.com",
    "abmairdropx@gmail.com",
    "meniyaanil23@gmail.com",
    "abmknox@gmail.com",
    "wwepubg@gmail.com",
    "abmstatusx@gmail.com",
    "askchampad@gmail.com",
    "newsreward15@gmail.com"
}

# 3. Token prefix blacklist
CLUSTER_1_BLACKLIST_TOKENS = {
    "f64shrkiytwa",
    "mjla45eyewoo",
    "uskczzaz2rrk",
    "fnzg44e5mrfn",
    "s5rrs3lzia5z",
    "ey5lo64l3bl7",
    "cstbtivprei3",
    "jm6w6hf6clkk",
    "ayxvrlmzd5kh"
}

# ----------------- CONFIGURATION -----------------
CLUSTER2_API_URL = "https://conso-zap2.onrender.com/api/add_account"
CHROME_USER_DATA = r"C:\Users\ABM ANIL\AppData\Local\Google\Chrome\User Data"

synced_tokens = set()

def scan_new_conso_accounts():
    if not os.path.exists(CHROME_USER_DATA):
        return []

    found = []
    for prof in os.listdir(CHROME_USER_DATA):
        # 1. Directory Level Shield
        if prof in EXCLUDED_PROFILES:
            continue

        prof_dir = os.path.join(CHROME_USER_DATA, prof)
        if not os.path.isdir(prof_dir):
            continue

        ext_dir = os.path.join(prof_dir, "Local Extension Settings", "bjibbmkefnaamkenamdppfengeepadpi")
        if not os.path.exists(ext_dir):
            continue

        for fname in os.listdir(ext_dir):
            if fname.endswith((".log", ".ldb")):
                fpath = os.path.join(ext_dir, fname)
                try:
                    with open(fpath, "rb") as f:
                        content = f.read().decode("latin-1", errors="ignore")

                    matches = re.finditer(r'refresh_token[\\]*"\s*:\s*[\\]*"([a-zA-Z0-9_\-\.]+)', content)
                    for m in matches:
                        token = m.group(1)
                        token_prefix = token[:12]

                        # Security check: Token blacklist
                        if any(token.startswith(bl) for bl in CLUSTER_1_BLACKLIST_TOKENS):
                            continue

                        # Extract surrounding email
                        surrounding = content[max(0, m.start() - 300):min(len(content), m.start() + 400)]
                        em_match = re.search(r'email[\\]*"\s*:\s*[\\]*"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', surrounding)
                        email = em_match.group(1).lower() if em_match else ""

                        # Security check: Email blacklist
                        if email in CLUSTER_1_BLACKLIST_EMAILS:
                            continue

                        # Extract display name if present
                        name_match = re.search(r'name[\\]*"\s*:\s*[\\]*"([^\\"]+)"', surrounding)
                        name = name_match.group(1) if name_match else (email.split("@")[0] if email else "Cluster 2 Node")

                        found.append({
                            "profile": prof,
                            "email": email,
                            "token": token,
                            "name": name
                        })
                except Exception:
                    pass

    return found

def sync_to_cluster2(account):
    token = account["token"]
    email = account.get("email", "")
    name = account.get("name", "")

    payload = {
        "refresh_token": token,
        "email": email,
        "name": name
    }

    try:
        r = requests.post(CLUSTER2_API_URL, json=payload, timeout=12)
        if r.status_code == 200:
            res = r.json()
            slot = res.get("slot", "Assigned")
            return True, slot
        else:
            return False, r.text
    except Exception as e:
        return False, str(e)

def main():
    print("=" * 65)
    print("      CONSO CLUSTER 2 - REAL-TIME AUTO-SYNC WATCHER v1.0")
    print("=" * 65)
    print("🛡️  SECURITY SHIELD ACTIVE:")
    print(f"   - {len(EXCLUDED_PROFILES)} Cluster 1 Chrome Profiles Quarantined & Blocked from scan.")
    print(f"   - {len(CLUSTER_1_BLACKLIST_EMAILS)} Cluster 1 Accounts on Permanent Blacklist.")
    print(f"   - Destination: {CLUSTER2_API_URL}")
    print("=" * 65)
    print("[*] Watching Chrome for NEW Conso logins in new profiles...")
    print("[*] (Make sure you use the dedicated Proxy Extension in each profile!)\n")

    synced_count = 0

    while True:
        try:
            candidates = scan_new_conso_accounts()
            for cand in candidates:
                tok = cand["token"]
                if tok in synced_tokens:
                    continue

                em = cand.get("email") or "Unknown"
                pr = cand.get("profile")
                print(f"\n[⚡ DETECTED] New Account in {pr}: {em}")
                print(f"[*] Syncing to Conso Zap Cluster 2 Render Engine...")

                ok, result = sync_to_cluster2(cand)
                if ok:
                    synced_tokens.add(tok)
                    synced_count += 1
                    print(f"✅ [SUCCESS] {em} synced to Slot '{result}' on Render!")
                    print(f"🚀 Live mining started through dedicated Oxylabs proxy!")
                    print(f"📊 Progress: {synced_count}/5 slots filled.\n")
                    try:
                        import winsound
                        winsound.Beep(1200, 300)
                    except Exception:
                        pass
                else:
                    print(f"[!] Sync failed or all slots occupied: {result}\n")

            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[*] Watcher stopped by user.")
            break
        except Exception as e:
            time.sleep(3)

if __name__ == "__main__":
    main()
