# -*- coding: utf-8 -*-
"""
地理設施調查 v3.7 網頁後端
輸入：座標 (lat, lng) 或地址 → 依 v3.7 規則查詢範圍內生活機能與嫌惡設施
逾時時可改用 Gemini 取得簡要說明（需 GOOGLE_API_KEY 或 GOOGLE_AI_STUDIO_API_KEY）
"""

import logging
import os
import json
import functools
import threading
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, render_template_string, session, redirect

# Firestore（有環境就啟用，否則 None）
try:
    from google.cloud import firestore as _firestore
    _db = None  # 延遲初始化
except ImportError:
    _firestore = None
    _db = None


def _get_db():
    """取得 Firestore client（延遲初始化）"""
    global _db
    if _db is not None:
        return _db
    if _firestore is None:
        return None
    try:
        _db = _firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT"))
        return _db
    except Exception as e:
        logging.warning("Survey: Firestore 初始化失敗，使用 GCS/本地 fallback: %s", e)
        return None

try:
    from pythonjsonlogger import jsonlogger
    _handler = logging.StreamHandler()
    _handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.root.handlers = [_handler]
    logging.root.setLevel(logging.INFO)
except ImportError:
    pass
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

try:
    from dotenv import load_dotenv
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _local_env = os.path.join(_app_dir, ".env")
    _central_env = os.path.join(_app_dir, "..", ".env")
    # 優先：ENV_FILE 環境變數指定路徑
    _env_file = os.environ.get("ENV_FILE")
    if _env_file and os.path.isfile(_env_file):
        load_dotenv(_env_file, override=False)
    # 其次：本專案目錄的 .env（本地覆蓋）
    elif os.path.isfile(_local_env):
        load_dotenv(_local_env, override=False)
    # 再次：上層 Projects/.env（集中管理）
    if os.path.isfile(_central_env):
        load_dotenv(_central_env, override=False)
except Exception:
    pass

from survey_v37 import run_survey, resolve_address, RADIUS_DEFAULT

app = Flask(__name__, static_folder="static", static_url_path="")
_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _secret and not os.environ.get("FLASK_DEBUG"):
    raise RuntimeError("FLASK_SECRET_KEY 未設定。生產環境必須設定此環境變數。")
app.secret_key = _secret or "dev-only-insecure-key"
# SameSite=None：Portal 跨站跳轉後瀏覽器才能正確帶 session cookie
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
ADMIN_EMAILS = [e.strip() for e in (os.environ.get("ADMIN_EMAILS") or "").split(",") if e.strip()]
FREE_SEARCH_LIMIT = int(os.environ.get("FREE_SEARCH_LIMIT", "3"))
PORTAL_URL = (os.environ.get("PORTAL_URL") or "").strip()
SERVICE_API_KEY = (os.environ.get("SERVICE_API_KEY") or "").strip()
TOKEN_SERIALIZER = URLSafeTimedSerializer(app.secret_key)
TOKEN_MAX_AGE = 300  # 5 分鐘，容忍 Cloud Run cold start


@app.route("/health")
def health_check():
    status = {"service": "survey", "status": "ok", "gcs": bool(GCS_BUCKET)}
    if GCS_BUCKET:
        try:
            _get_gcs_bucket()
            status["gcs_reachable"] = True
        except Exception:
            status["gcs_reachable"] = False
    return jsonify(status)


@app.route("/api/config")
def api_config():
    """回傳前端所需的公開設定"""
    from survey_v37 import _get_all_api_keys, _find_working_key, _working_key
    key = _working_key
    if not key:
        key = _find_working_key()
    return jsonify({"maps_api_key": key or "", "oauth_client_id": GOOGLE_OAUTH_CLIENT_ID, "portal_url": PORTAL_URL})


@app.route("/api/debug")
def api_debug():
    """除錯端點：在瀏覽器打開 /api/debug 就能看到系統狀態"""
    from survey_v37 import _get_all_api_keys, _find_working_key, _working_key

    keys = _get_all_api_keys()
    info = {
        "method": "Google Places API (New) — Text Search",
        "candidate_keys": [f"{k[:8]}...{k[-4:]}" for k in keys],
        "working_key": f"{_working_key[:8]}...{_working_key[-4:]}" if _working_key else "(尚未測試)",
        "env_PORT": os.environ.get("PORT", "未設定（預設 5000）"),
    }

    if not _working_key:
        found = _find_working_key()
        info["working_key"] = f"{found[:8]}...{found[-4:]}" if found else "❌ 無可用 key"
        info["test_result"] = "OK" if found else "FAILED — 所有 key 都無法使用 Places API (New)"

    return jsonify(info)


def _gemini_api_key():
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")


def _gemini_summary(address_display: str, lat: float, lng: float, radius_m: int) -> str:
    """呼叫 Gemini 產生該地點周邊生活機能／嫌惡設施簡要說明。"""
    api_key = _gemini_api_key()
    if not api_key:
        raise ValueError("未設定 Gemini API Key（請設 GOOGLE_API_KEY 或 GOOGLE_AI_STUDIO_API_KEY，或設定 ENV_FILE 指向含該 key 的 .env）")
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""你是一位房仲用的地理設施分析助手。請針對以下地點，用 200–300 字簡要說明：
- 周邊生活機能（如便利商店、超市、學校、醫院、公園、大眾運輸等）
- 嫌惡設施（如宮廟、殯葬、加油站、高壓電塔、工廠等）
- 宗教類設施若與生活圈重疊也請提及

地點：{address_display}
經緯度：{lat}, {lng}
查詢半徑：{radius_m} 公尺

請直接給出說明，不要重複題目。若無法取得即時資料，可依該區一般狀況簡述。"""
    response = model.generate_content(prompt)
    if not response or not response.text:
        raise ValueError("Gemini 未回傳內容")
    return response.text.strip()


_APP_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DIR = os.path.join(_APP_DIR, "users")
os.makedirs(USERS_DIR, exist_ok=True)
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
# 歷史在 GCS 的前綴；改版勿隨意變更，僅能透過遷移腳本與新路徑並存
HISTORY_GCS_PREFIX = (os.environ.get("HISTORY_GCS_PREFIX") or "history").strip().rstrip("/")
_gcs_client = None
_gcs_bucket = None

def _get_gcs_bucket():
    global _gcs_client, _gcs_bucket
    if _gcs_bucket is None and GCS_BUCKET:
        from google.cloud import storage
        _gcs_client = storage.Client()
        _gcs_bucket = _gcs_client.bucket(GCS_BUCKET)
    return _gcs_bucket

def _gcs_read(path):
    bucket = _get_gcs_bucket()
    if not bucket:
        return None
    blob = bucket.blob(path)
    if not blob.exists():
        return None
    return blob.download_as_text(encoding="utf-8")

def _gcs_write(path, data_str):
    bucket = _get_gcs_bucket()
    if not bucket:
        return False
    blob = bucket.blob(path)
    blob.upload_from_string(data_str, content_type="application/json")
    return True

def _gcs_delete(path):
    bucket = _get_gcs_bucket()
    if not bucket:
        return False
    blob = bucket.blob(path)
    if blob.exists():
        blob.delete()
    return True

def _gcs_list(prefix):
    bucket = _get_gcs_bucket()
    if not bucket:
        return []
    return [b.name for b in bucket.list_blobs(prefix=prefix)]

# ── Local fallback paths ──
FEEDBACK_FILE = os.environ.get("FEEDBACK_FILE") or os.path.join(_APP_DIR, "feedback.json")
GENERAL_FEEDBACK_FILE = os.environ.get("GENERAL_FEEDBACK_FILE") or os.path.join(_APP_DIR, "general_feedback.json")
HISTORY_DIR = os.environ.get("HISTORY_DIR") or os.path.join(_APP_DIR, "history")
os.makedirs(HISTORY_DIR, exist_ok=True)


def _safe_email(email):
    """用於路徑的 email 安全字串（與 Portal/Library 一致）"""
    return email.replace("@", "_at_").replace(".", "_") if email else ""


def _history_user_prefix(safe_email):
    """歷史紀錄在 GCS/本機的「每用戶」前綴。"""
    if not safe_email:
        return None
    return f"users/{safe_email}/"


def _history_path_for_user(safe_email, history_id, for_gcs=True):
    """一筆歷史的儲存路徑。for_gcs=True 為 GCS 相對路徑，False 為本機路徑。"""
    safe_id = os.path.basename(history_id).replace("..", "").strip()
    if not safe_id.endswith(".json"):
        safe_id = f"{safe_id}.json"
    prefix = _history_user_prefix(safe_email)
    if not prefix:
        if for_gcs and GCS_BUCKET:
            return f"{HISTORY_GCS_PREFIX}/{safe_id}"
        return os.path.join(HISTORY_DIR, safe_id)
    if for_gcs and GCS_BUCKET:
        return f"{HISTORY_GCS_PREFIX}/{prefix}{safe_id}"
    os.makedirs(os.path.join(HISTORY_DIR, prefix.replace("/", os.sep).rstrip(os.sep)), exist_ok=True)
    return os.path.join(HISTORY_DIR, prefix.replace("/", os.sep), safe_id)



_feedback_lock = threading.Lock()


def _atomic_write(fpath, data_str):
    """原子寫入：先寫 .tmp，fsync 後再 os.replace，讀取時永遠是完整檔案。"""
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data_str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, fpath)


def _save_feedback(data):
    """整體覆寫 feedback（fallback 用）"""
    data_str = json.dumps(data, ensure_ascii=False, indent=2)
    with _feedback_lock:
        if GCS_BUCKET:
            _gcs_write("feedback.json", data_str)
        else:
            _atomic_write(FEEDBACK_FILE, data_str)


def _apply_feedback(result):
    """套用使用者反饋到查詢結果：過濾無效、覆寫 notable/nuisance、附加意見"""
    from survey_v37 import NUISANCE_CATEGORIES
    fb = _load_feedback()
    if not fb:
        return result

    filtered = []
    for f in result.get("facilities", []):
        pid = f.get("place_id", "")
        entry = fb.get(pid)
        if not entry:
            filtered.append(f)
            continue
        action = entry.get("action")
        if action == "invalid":
            continue
        if action == "notable":
            f["notable"] = True
            f["feedback"] = "notable"
        elif action == "normal":
            f["notable"] = False
            f["feedback"] = "normal"
        elif action == "nuisance":
            if f["category"] not in NUISANCE_CATEGORIES:
                f["original_category"] = f["category"]
            f["category"] = entry.get("nuisance_cat", "嫌惡設施(自訂)")
            f["notable"] = False
            f["feedback"] = "nuisance"
        if entry.get("comment"):
            f["user_comment"] = entry["comment"]
        if entry.get("severity_override"):
            f["severity_override"] = entry["severity_override"]
        if not f.get("feedback") and (entry.get("comment") or entry.get("severity_override")):
            f["feedback"] = "commented"
        filtered.append(f)

    result["facilities"] = filtered
    result["total_count"] = len(filtered)
    result["feedback_applied"] = True
    return result


def _regenerate_summary(result, address_display, radius_m):
    """反饋套用後重新生成 summary 和評分（確保總結與設施列表一致）"""
    from survey_v37 import (generate_summary, calculate_weighted_score,
                            assess_nuisance_impact, religious_impact_notes,
                            analyze_missing, generate_persona_summaries,
                            NUISANCE_CATEGORIES, RELIGIOUS_CATEGORIES)
    facilities = result.get("facilities", [])
    stats = {}
    for f in facilities:
        stats[f["category"]] = stats.get(f["category"], 0) + 1

    nuisance_raw = [f for f in facilities if f["category"] in NUISANCE_CATEGORIES]
    religious = [f for f in facilities if f["category"] in RELIGIOUS_CATEGORIES]

    score_result = calculate_weighted_score(facilities, nuisance_raw)
    score = score_result["score"]
    nuisance_impact = assess_nuisance_impact(nuisance_raw)
    rel_notes = religious_impact_notes(religious)
    missing = analyze_missing(facilities, facilities, radius_m)
    personas = generate_persona_summaries(facilities, missing)

    summary = generate_summary(
        facilities, stats, score, nuisance_raw, religious, address_display, radius_m,
        nuisance_impact=nuisance_impact, religious_notes=rel_notes,
        missing=missing, personas=personas,
        score_result=score_result,
    )

    result["stats"] = stats
    result["score"] = score
    result["stars"] = "⭐" * int(round(score)) + "☆" * (5 - int(round(score)))
    result["convenience_score"] = score_result["convenience_score"]
    result["nuisance_score"] = score_result["nuisance_score"]
    result["recommend_thumbs"] = score_result["recommend_thumbs"]
    result["thumbs_display"] = score_result["thumbs_display"]
    result["score_result"] = score_result
    result["nuisance"] = nuisance_raw
    result["nuisance_impact"] = nuisance_impact
    result["religious"] = religious
    result["religious_notes"] = rel_notes
    result["missing"] = missing
    result["personas"] = personas
    result["summary"] = summary


# ── Auth helpers ──

def _load_user(email):
    """讀取用戶資料。優先 Firestore（Portal 是 source of truth），否則 GCS/本地。"""
    db = _get_db()
    if db and email:
        try:
            doc = db.collection("users").document(email).get()
            if doc.exists:
                return doc.to_dict()
        except Exception as e:
            logging.warning("Survey: Firestore 讀取用戶失敗: %s", e)

    # Fallback：GCS / 本地
    safe = email.replace("@", "_at_").replace(".", "_")
    if GCS_BUCKET:
        raw = _gcs_read(f"users/{safe}.json")
        if raw:
            try: return json.loads(raw)
            except Exception: return None
        return None
    fpath = os.path.join(USERS_DIR, f"{safe}.json")
    if os.path.isfile(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_user(user_data):
    """儲存用戶資料。優先 Firestore，否則 GCS/本地。"""
    email = user_data.get("email", "")
    if not email:
        return

    db = _get_db()
    if db:
        try:
            db.collection("users").document(email).set(user_data)
            return
        except Exception as e:
            logging.warning("Survey: Firestore 儲存用戶失敗，改用 GCS/本地: %s", e)

    # Fallback
    safe = email.replace("@", "_at_").replace(".", "_")
    data_str = json.dumps(user_data, ensure_ascii=False, indent=2)
    if GCS_BUCKET:
        _gcs_write(f"users/{safe}.json", data_str)
    else:
        fpath = os.path.join(USERS_DIR, f"{safe}.json")
        _atomic_write(fpath, data_str)


def _is_admin(email):
    return email in ADMIN_EMAILS


def _get_current_user():
    email = session.get("user_email")
    if not email:
        return None
    return _load_user(email)


# SERVICE_API_KEY 已在第 82 行定義，此處不再重複


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if SERVICE_API_KEY:
            auth_header = request.headers.get("Authorization", "")
            import hmac as _hmac
            if _hmac.compare_digest(auth_header, f"Bearer {SERVICE_API_KEY}"):
                return f(*args, **kwargs)
        if not session.get("user_email"):
            return jsonify({"error": "未登入", "login_required": True}), 401
        return f(*args, **kwargs)
    return decorated


def _portal_check_and_deduct(email, tool_id="survey"):
    """呼叫 Portal 檢查並扣一次使用。若未設定 PORTAL_URL 或 SERVICE_API_KEY 則回傳 (True, None) 表示略過（由本地邏輯處理）。"""
    if not PORTAL_URL or not SERVICE_API_KEY:
        return True, None
    try:
        import requests as _req
        r = _req.post(
            f"{PORTAL_URL.rstrip('/')}/api/usage/check-and-deduct",
            json={"email": email, "tool_id": tool_id},
            headers={"X-Service-Key": SERVICE_API_KEY},
            timeout=10,
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("ok"):
            return True, data
        return False, data.get("reason", "用量檢查失敗")
    except Exception as e:
        return False, str(e)


def _portal_get_usage(email):
    """向 Portal 取得目前用量與餘額。若未設定則回傳 None。"""
    if not PORTAL_URL or not SERVICE_API_KEY:
        return None
    try:
        import requests as _req
        r = _req.get(
            f"{PORTAL_URL.rstrip('/')}/api/usage",
            params={"email": email},
            headers={"X-Service-Key": SERVICE_API_KEY},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        email = session.get("user_email")
        if not email:
            return jsonify({"error": "未登入", "login_required": True}), 401
        if not _is_admin(email):
            return jsonify({"error": "無管理權限"}), 403
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ──

@app.route("/auth/google", methods=["POST"])
def auth_google():
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    data = request.get_json() or {}
    token = data.get("credential", "")
    if not token:
        return jsonify({"error": "缺少 credential"}), 400
    if not GOOGLE_OAUTH_CLIENT_ID:
        return jsonify({"error": "伺服器未設定 OAuth Client ID"}), 500
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_OAUTH_CLIENT_ID)
    except Exception as e:
        return jsonify({"error": f"驗證失敗：{e}"}), 401

    email = idinfo.get("email", "")
    from datetime import datetime
    user = _load_user(email) or {
        "email": email,
        "name": idinfo.get("name", ""),
        "picture": idinfo.get("picture", ""),
        "first_login": datetime.now().isoformat(),
        "plan": "free",
        "usage": {"search_count": 0},
    }
    user["name"] = idinfo.get("name", user.get("name", ""))
    user["picture"] = idinfo.get("picture", user.get("picture", ""))
    user["last_login"] = datetime.now().isoformat()
    _save_user(user)

    session["user_email"] = email
    session["user_name"] = user["name"]
    session["user_picture"] = user.get("picture", "")

    is_admin = _is_admin(email)
    usage = user.get("usage", {})
    limit = None if is_admin else FREE_SEARCH_LIMIT

    return jsonify({
        "ok": True,
        "email": email,
        "name": user["name"],
        "picture": user.get("picture", ""),
        "is_admin": is_admin,
        "plan": user.get("plan", "free"),
        "search_count": usage.get("search_count", 0),
        "search_limit": limit,
    })


@app.route("/auth/me")
def auth_me():
    email = session.get("user_email")
    if not email:
        return jsonify({"logged_in": False})
    user = _load_user(email)
    if not user:
        session.clear()
        return jsonify({"logged_in": False})
    is_admin = _is_admin(email)
    portal_usage = _portal_get_usage(email)
    usage = (portal_usage.get("usage", {}) if portal_usage else None) or user.get("usage", {})
    # 取得點數（優先從 portal_usage，其次本地 user）
    points = None
    if portal_usage:
        points = portal_usage.get("points")
    if points is None:
        points = user.get("points", user.get("balance_uses", 0) or 0)
    return jsonify({
        "logged_in": True,
        "email": email,
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
        "is_admin": is_admin,
        "plan": user.get("plan", "free"),
        "points": points,
        "search_count": usage.get("search_count", 0),
        "search_limit": None if is_admin else FREE_SEARCH_LIMIT,
    })


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    if PORTAL_URL:
        return jsonify({"ok": True, "redirect": PORTAL_URL})
    return jsonify({"ok": True})


@app.route("/auth/portal-return-token")
def auth_portal_return_token():
    """向 Portal 申請回家 token，讓使用者免重新登入直接回入口。"""
    email = session.get("user_email")
    if not email or not PORTAL_URL or not SERVICE_API_KEY:
        return jsonify({"portal_url": PORTAL_URL or "/"})
    try:
        import requests as _req
        r = _req.get(
            f"{PORTAL_URL.rstrip('/')}/auth/return-token",
            params={"email": email},
            headers={"X-Service-Key": SERVICE_API_KEY},
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("token", "")
            if token:
                from urllib.parse import quote as _quote
                return jsonify({"portal_url": f"{PORTAL_URL.rstrip('/')}/auth/home-login?token={_quote(token, safe='')}"})
    except Exception:
        pass
    return jsonify({"portal_url": PORTAL_URL})


@app.route("/auth/portal-login")
def auth_portal_login():
    """Accept a signed token from Portal and create a local session."""
    token = request.args.get("token", "")
    if not token:
        return redirect(PORTAL_URL or "/")
    try:
        payload = TOKEN_SERIALIZER.loads(token, salt="portal-sso", max_age=TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return redirect(PORTAL_URL or "/")
    except Exception as e:
        logging.exception("portal-login token load failed: %s", e)
        return redirect(PORTAL_URL or "/")

    email = payload.get("email", "")
    if not email:
        return redirect(PORTAL_URL or "/")

    try:
        from datetime import datetime
        user = _load_user(email) or {
            "email": email, "name": payload.get("name", ""), "picture": payload.get("picture", ""),
            "first_login": datetime.now().isoformat(), "plan": "free",
            "usage": {"search_count": 0},
        }
        user["last_login"] = datetime.now().isoformat()
        _save_user(user)

        session["user_email"] = email
        session["user_name"] = payload.get("name", "")
        session["user_picture"] = payload.get("picture", "")
        session.modified = True

        # 直接 serve 靜態首頁（不做任何 redirect），Set-Cookie 與 HTML 在同一個 response
        # 避免 Chrome SameSite 問題：跨站 redirect 後瀏覽器帶不到剛設的 cookie
        return send_from_directory("static", "index.html")
    except Exception as e:
        logging.exception("portal-login session setup failed: %s", e)
        return redirect(PORTAL_URL or "/")


FEEDBACK_ADMIN_PAGE = """
<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>反饋管理 - Survey</title>
<style>
  :root{--bg:#f5f5f7;--card:#fff;--border:#d2d2d7;--accent:#007aff;--danger:#ff3b30;--muted:#8e8e93;--text:#1d1d1f}
  *{box-sizing:border-box}
  body{font-family:-apple-system,sans-serif;background:var(--bg);margin:0;padding:16px;color:var(--text)}
  .container{max-width:800px;margin:0 auto}
  h1{font-size:20px;text-align:center;margin-bottom:8px}
  .tabs{display:flex;gap:8px;margin-bottom:16px;justify-content:center}
  .tab{padding:8px 20px;border-radius:10px;border:1px solid var(--border);background:var(--card);cursor:pointer;font-size:14px;font-weight:600}
  .tab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .panel{display:none}.panel.active{display:block}
  .card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 6px rgba(0,0,0,.05);border:1px solid var(--border)}
  .card-title{font-weight:700;font-size:14px;margin-bottom:6px}
  .tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;margin-right:4px}
  .tag-invalid{background:#fee;color:var(--danger)}.tag-notable{background:#e8f5e9;color:#2e7d32}
  .tag-nuisance{background:#fff3e0;color:#e65100}.tag-normal{background:#f5f5f5;color:#666}
  .tag-commented{background:#e3f2fd;color:#1565c0}
  .meta{font-size:12px;color:var(--muted);margin-top:4px}
  .comment{background:#f9f9fb;border-radius:8px;padding:8px 10px;margin-top:6px;font-size:13px;border-left:3px solid var(--accent)}
  .severity{font-size:13px;margin-top:4px}
  .empty{text-align:center;color:var(--muted);padding:40px;font-size:14px}
  .stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;justify-content:center}
  .stat{background:var(--card);border-radius:10px;padding:10px 16px;border:1px solid var(--border);text-align:center;min-width:80px}
  .stat-num{font-size:22px;font-weight:700;color:var(--accent)}.stat-label{font-size:11px;color:var(--muted)}
  .back{display:inline-block;margin-bottom:12px;color:var(--accent);text-decoration:none;font-size:13px}
</style></head><body>
<div class="container">
  <a href="/" class="back">← 回到調查工具</a>
  <h1>📊 使用者反饋管理</h1>
  <div id="stats" class="stats"></div>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('facility')">設施反饋</div>
    <div class="tab" onclick="switchTab('general')">通用意見</div>
  </div>
  <div id="facility-panel" class="panel active"></div>
  <div id="general-panel" class="panel"></div>
</div>
<script>
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', (name==='facility'?i===0:i===1)));
  document.getElementById('facility-panel').classList.toggle('active', name==='facility');
  document.getElementById('general-panel').classList.toggle('active', name==='general');
}

const ACTION_LABELS = {invalid:'標記無效',notable:'標為重要',normal:'改為普通',nuisance:'標為嫌惡',commented:'已留言'};
const ACTION_TAGS = {invalid:'tag-invalid',notable:'tag-notable',normal:'tag-normal',nuisance:'tag-nuisance',commented:'tag-commented'};
const SEV_EMOJI = {high:'🔴 高',medium:'🟡 中',low:'🟢 低'};

async function loadAll() {
  const [fbRes, gfRes] = await Promise.all([fetch('/api/feedback'), fetch('/api/general-feedback')]);
  const fb = await fbRes.json();
  const gf = await gfRes.json();
  renderStats(fb, gf);
  renderFacility(fb);
  renderGeneral(gf);
}

function renderStats(fb, gf) {
  const entries = Object.values(fb);
  const counts = {invalid:0, notable:0, nuisance:0, normal:0, commented:0};
  entries.forEach(e => { if(e.action && counts[e.action]!==undefined) counts[e.action]++; });
  const commented = entries.filter(e => e.comment).length;
  document.getElementById('stats').innerHTML =
    `<div class="stat"><div class="stat-num">${entries.length}</div><div class="stat-label">設施反饋</div></div>`+
    `<div class="stat"><div class="stat-num">${counts.invalid}</div><div class="stat-label">無效</div></div>`+
    `<div class="stat"><div class="stat-num">${counts.notable}</div><div class="stat-label">重要</div></div>`+
    `<div class="stat"><div class="stat-num">${counts.nuisance}</div><div class="stat-label">嫌惡</div></div>`+
    `<div class="stat"><div class="stat-num">${commented}</div><div class="stat-label">有留言</div></div>`+
    `<div class="stat"><div class="stat-num">${gf.length}</div><div class="stat-label">通用意見</div></div>`;
}

function renderFacility(fb) {
  const panel = document.getElementById('facility-panel');
  const entries = Object.entries(fb);
  if (!entries.length) { panel.innerHTML = '<div class="empty">尚無設施反饋</div>'; return; }
  entries.sort((a,b) => (b[1].updated_at||'').localeCompare(a[1].updated_at||''));
  panel.innerHTML = entries.map(([pid, e]) => {
    const action = e.action || 'commented';
    const tagCls = ACTION_TAGS[action] || 'tag-commented';
    let html = `<div class="card">`;
    html += `<div class="card-title">${e.name || pid} <span class="tag ${tagCls}">${ACTION_LABELS[action]||action}</span></div>`;
    if (e.address) {
      html += `<div class="meta" style="margin-bottom:2px;">📍 查詢地址：<b>${e.address}</b>`;
      if (e.history_id) html += ` <a href="/view/${encodeURIComponent(e.history_id)}" target="_blank" style="color:var(--accent);text-decoration:none;">🔗 查看報告</a>`;
      html += `</div>`;
    } else if (e.history_id) {
      html += `<div class="meta"><a href="/view/${encodeURIComponent(e.history_id)}" target="_blank" style="color:var(--accent);text-decoration:none;">🔗 查看當時報告</a></div>`;
    }
    if (e.updated_at) html += `<div class="meta">更新：${new Date(e.updated_at).toLocaleString('zh-TW')}</div>`;
    if (e.severity_override) html += `<div class="severity">嚴重度調整：${SEV_EMOJI[e.severity_override]||e.severity_override}</div>`;
    if (e.comment) html += `<div class="comment">💬 ${e.comment}</div>`;
    html += `<div class="meta" style="margin-top:4px;font-size:11px;color:#bbb;">ID: ${pid}</div>`;
    html += `</div>`;
    return html;
  }).join('');
}

function renderGeneral(gf) {
  const panel = document.getElementById('general-panel');
  if (!gf.length) { panel.innerHTML = '<div class="empty">尚無通用意見</div>'; return; }
  gf.sort((a,b) => (b.created_at||'').localeCompare(a.created_at||''));
  panel.innerHTML = gf.map(e => {
    let html = `<div class="card">`;
    if (e.category) html += `<span class="tag tag-commented">${e.category}</span>`;
    html += `<div style="margin-top:4px;font-size:14px;">${e.text}</div>`;
    if (e.created_at) html += `<div class="meta">${new Date(e.created_at).toLocaleString('zh-TW')}</div>`;
    html += `</div>`;
    return html;
  }).join('');
}

loadAll();
</script></body></html>
"""


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/view/<history_id>")
def view_shared(history_id):
    """共享連結：載入歷史紀錄並直接顯示結果"""
    return send_from_directory("static", "index.html")


@app.route("/admin/feedback")
def admin_feedback():
    """反饋管理頁面（需管理員登入）"""
    if not session.get("user_email") or not _is_admin(session["user_email"]):
        return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><title>需要登入</title></head>
        <body style="display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
        <div style="text-align:center;"><h2>需要管理員登入</h2><a href="/">回首頁登入</a></div></body></html>""")
    return render_template_string(FEEDBACK_ADMIN_PAGE)


VALID_THEME_STYLES = ["navy", "forest", "amber", "minimal", "rose", "oled"]

@app.route("/api/theme", methods=["GET"])
def api_theme_get():
    """取得全局外觀風格（從 Firestore system_settings/theme 讀取）。"""
    db = _get_db()
    style, mode = "navy", None
    if db:
        try:
            doc = db.collection("system_settings").document("theme").get()
            if doc.exists:
                d = doc.to_dict()
                style = d.get("style", "navy")
                mode = d.get("mode")
        except Exception:
            pass
    return jsonify({"style": style, "mode": mode})


@app.route("/api/theme", methods=["POST"])
def api_theme_set():
    """設定外觀：管理員可改色系(style)，任何登入用戶可改深淺色(mode)。"""
    email = session.get("user_email", "")
    if not email:
        return jsonify({"error": "請先登入"}), 401
    data = request.get_json(silent=True) or {}
    update = {}
    if "style" in data:
        if not _is_admin(email):
            return jsonify({"error": "無管理權限"}), 403
        style = data["style"]
        if style not in VALID_THEME_STYLES:
            return jsonify({"error": "無效風格"}), 400
        update["style"] = style
    if "mode" in data:
        mode = data["mode"]
        if mode in ("dark", "light", "system"):
            update["mode"] = mode
    if update:
        db = _get_db()
        if db:
            try:
                db.collection("system_settings").document("theme").set(update, merge=True)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["GET"])
def api_feedback_get():
    return jsonify(_load_feedback())


@app.route("/api/feedback", methods=["POST"])
def api_feedback_post():
    """設施反饋（含意見文字、嚴重度調整）"""
    data = request.get_json() or {}
    place_id = data.get("place_id", "").strip()
    action = data.get("action", "").strip()
    valid_actions = ("invalid", "notable", "normal", "nuisance", "reset",
                     "comment", "severity")
    if not place_id or action not in valid_actions:
        return jsonify({"error": f"需要 place_id 和 action（{'/'.join(valid_actions)}）"}), 400

    from datetime import datetime
    fb = _load_feedback()
    ctx_history = data.get("history_id", "")
    ctx_address = data.get("address", "")

    if action == "reset":
        # 刪除該筆 feedback
        db = _get_db()
        if db:
            try:
                db.collection("survey_feedback").document(place_id).delete()
            except Exception as e:
                logging.warning("Survey: Firestore 刪除 feedback 失敗: %s", e)
                fb.pop(place_id, None)
                _save_feedback(fb)
        else:
            fb.pop(place_id, None)
            _save_feedback(fb)
    elif action == "comment":
        entry = fb.get(place_id, {})
        entry["name"] = data.get("name", entry.get("name", ""))
        entry["comment"] = data.get("comment", "")
        entry["updated_at"] = datetime.now().isoformat()
        if ctx_history: entry["history_id"] = ctx_history
        if ctx_address: entry["address"] = ctx_address
        _save_feedback_entry(place_id, entry)
    elif action == "severity":
        entry = fb.get(place_id, {})
        entry["name"] = data.get("name", entry.get("name", ""))
        entry["severity_override"] = data.get("severity", "")
        entry["updated_at"] = datetime.now().isoformat()
        if ctx_history: entry["history_id"] = ctx_history
        if ctx_address: entry["address"] = ctx_address
        _save_feedback_entry(place_id, entry)
    else:
        entry = {
            "action": action,
            "name": data.get("name", ""),
            "nuisance_cat": data.get("nuisance_cat", "嫌惡設施(自訂)"),
            "updated_at": datetime.now().isoformat(),
        }
        if ctx_history: entry["history_id"] = ctx_history
        if ctx_address: entry["address"] = ctx_address
        _save_feedback_entry(place_id, entry)
    return jsonify({"ok": True})


def _load_general_feedback():
    if GCS_BUCKET:
        raw = _gcs_read("general_feedback.json")
        if raw:
            try: return json.loads(raw)
            except Exception: return []
        return []
    if os.path.isfile(GENERAL_FEEDBACK_FILE):
        try:
            with open(GENERAL_FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


@app.route("/api/general-feedback", methods=["GET"])
def api_general_feedback_get():
    """列出所有通用意見"""
    return jsonify(_load_general_feedback())


@app.route("/api/general-feedback", methods=["POST"])
def api_general_feedback():
    """通用意見反饋"""
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "請輸入意見內容"}), 400
    from datetime import datetime
    entries = _load_general_feedback()
    entries.append({
        "text": text,
        "category": data.get("category", ""),
        "created_at": datetime.now().isoformat(),
    })
    data_str = json.dumps(entries, ensure_ascii=False, indent=2)
    if GCS_BUCKET:
        _gcs_write("general_feedback.json", data_str)
    else:
        _atomic_write(GENERAL_FEEDBACK_FILE, data_str)
    return jsonify({"ok": True, "total": len(entries)})


@app.route("/api/resolve-address", methods=["POST"])
def api_resolve_address():
    """POST { "address": "台北市信義區信義路五段7號" } → { "lat": ..., "lng": ... }"""
    data = request.get_json() or {}
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "請提供 address"}), 400
    coords = resolve_address(address)
    if coords:
        return jsonify({"lat": coords[0], "lng": coords[1]})
    return jsonify({"error": "無法解析地址"}), 400


@app.route("/api/search", methods=["POST"])
@login_required
def api_search():
    """POST body: { "lat": 22.76, "lng": 121.15 }，可選 "radius_m": 350, "address_display": "..." """
    email = session.get("user_email", "")
    user = _load_user(email) if email else None

    ok, reason_or_data = _portal_check_and_deduct(email, "survey")
    if not ok:
        return jsonify({"error": reason_or_data or "用量已達上限", "usage_exceeded": True}), 403

    if reason_or_data is None and user and not _is_admin(email):
        points = int(user.get("points", user.get("balance_uses", 0) or 0))
        if points < 1:
            return jsonify({"error": "點數不足，請至入口儲值。", "usage_exceeded": True}), 403
        user["points"] = points - 1
        # search_count 僅在查詢成功後累加，此處先暫存扣點，不累加 search_count

    data = request.get_json() or {}
    lat = data.get("lat")
    lng = data.get("lng")
    radius_m = data.get("radius_m") or RADIUS_DEFAULT
    address_display = (data.get("address_display") or "").strip()

    if lat is None or lng is None:
        return jsonify({"error": "請提供 lat + lng（請先在地圖上設定座標）"}), 400
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return jsonify({"error": "經緯度格式錯誤"}), 400

    if not address_display:
        address_display = f"{lat}, {lng}"

    try:
        result = run_survey(lat, lng, radius_m=radius_m)
        result["address_display"] = address_display
        result = _apply_feedback(result)
        _regenerate_summary(result, address_display, radius_m)

        # 查詢成功後才扣點並累加 search_count（確保只加一次）
        if reason_or_data is None and user and not _is_admin(email):
            usage = user.get("usage", {})
            usage["search_count"] = usage.get("search_count", 0) + 1
            user["usage"] = usage
            _save_user(user)

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh-summary", methods=["POST"])
def api_refresh_summary():
    """依據前端目前的設施資料（含反饋修改）重新產生總結與評分，不重新查詢 Google API"""
    data = request.get_json() or {}
    facilities = data.get("facilities")
    address_display = data.get("address_display", "")
    radius_m = data.get("radius_m", RADIUS_DEFAULT)

    if not facilities or not isinstance(facilities, list):
        return jsonify({"error": "缺少 facilities 資料"}), 400

    try:
        result = {"facilities": facilities}
        _regenerate_summary(result, address_display, radius_m)
        return jsonify({
            "summary": result.get("summary", ""),
            "score": result.get("score"),
            "stars": result.get("stars"),
            "convenience_score": result.get("convenience_score"),
            "nuisance_score": result.get("nuisance_score"),
            "recommend_thumbs": result.get("recommend_thumbs"),
            "thumbs_display": result.get("thumbs_display"),
            "score_result": result.get("score_result"),
            "nuisance_impact": result.get("nuisance_impact"),
            "missing": result.get("missing"),
            "personas": result.get("personas"),
        })
    except Exception as e:
        app.logger.error("refresh-summary error: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── History Firestore helpers ──

def _survey_history_list(email):
    """列出用戶的 Survey 歷史摘要，最新在前。優先 Firestore，否則 GCS/本地。"""
    safe = _safe_email(email)
    db = _get_db()
    if db and email:
        try:
            docs = db.collection("users").document(email).collection("survey_history") \
                     .order_by("created_at", direction=_firestore.Query.DESCENDING) \
                     .limit(50).stream()
            items = []
            for doc in docs:
                rec = doc.to_dict()
                full_summary = rec.get("summary", "")
                items.append({
                    "id": doc.id,
                    "address": rec.get("address_display", ""),
                    "custom_title": rec.get("custom_title", ""),
                    "lat": rec.get("lat"),
                    "lng": rec.get("lng"),
                    "radius_m": rec.get("radius_m"),
                    "score": rec.get("score"),
                    "convenience_score": rec.get("convenience_score"),
                    "recommend_thumbs": rec.get("recommend_thumbs"),
                    "thumbs_display": rec.get("thumbs_display"),
                    "total_count": rec.get("total_count"),
                    "created_at": rec.get("created_at", ""),
                    "summary_preview": full_summary[:80] if full_summary else "",
                    "share_path": f"{safe}/{doc.id}",
                })
            return items
        except Exception as e:
            logging.warning("Survey: Firestore 列出歷史失敗: %s", e)

    # Fallback：GCS / 本地
    prefix = _history_user_prefix(safe)
    items = []
    if GCS_BUCKET and prefix:
        blobs = sorted(_gcs_list(HISTORY_GCS_PREFIX + "/" + prefix), reverse=True)
        for bname in blobs:
            if not bname.endswith(".json"):
                continue
            try:
                raw = _gcs_read(bname)
                if not raw:
                    continue
                rec = json.loads(raw)
                fid = bname.replace(HISTORY_GCS_PREFIX + "/" + prefix, "").replace(".json", "")
                full_summary = rec.get("summary", "")
                items.append({
                    "id": fid,
                    "address": rec.get("address_display", ""),
                    "custom_title": rec.get("custom_title", ""),
                    "lat": rec.get("lat"),
                    "lng": rec.get("lng"),
                    "radius_m": rec.get("radius_m"),
                    "score": rec.get("score"),
                    "convenience_score": rec.get("convenience_score"),
                    "recommend_thumbs": rec.get("recommend_thumbs"),
                    "thumbs_display": rec.get("thumbs_display"),
                    "total_count": rec.get("total_count"),
                    "created_at": rec.get("created_at", ""),
                    "summary_preview": full_summary[:80] if full_summary else "",
                    "share_path": f"{safe}/{fid}",
                })
            except Exception:
                continue
    elif prefix:
        user_dir = os.path.join(HISTORY_DIR, "users", safe)
        if os.path.isdir(user_dir):
            for fname in sorted(os.listdir(user_dir), reverse=True):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(user_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                    fid = fname.replace(".json", "")
                    full_summary = rec.get("summary", "")
                    items.append({
                        "id": fid,
                        "address": rec.get("address_display", ""),
                        "custom_title": rec.get("custom_title", ""),
                        "lat": rec.get("lat"),
                        "lng": rec.get("lng"),
                        "radius_m": rec.get("radius_m"),
                        "score": rec.get("score"),
                        "convenience_score": rec.get("convenience_score"),
                        "recommend_thumbs": rec.get("recommend_thumbs"),
                        "thumbs_display": rec.get("thumbs_display"),
                        "total_count": rec.get("total_count"),
                        "created_at": rec.get("created_at", ""),
                        "summary_preview": full_summary[:80] if full_summary else "",
                        "share_path": f"{safe}/{fid}",
                    })
                except Exception:
                    continue
    return items


def _survey_history_get(email, history_id):
    """讀取一筆 Survey 歷史完整資料。優先 Firestore，否則 GCS/本地。"""
    safe = _safe_email(email)
    db = _get_db()
    if db and email:
        try:
            doc = db.collection("users").document(email).collection("survey_history").document(history_id).get()
            if doc.exists:
                data = doc.to_dict()
                data.pop("_id", None)
                return data
        except Exception as e:
            logging.warning("Survey: Firestore 讀取歷史失敗: %s", e)

    # Fallback
    safe_id = os.path.basename(history_id).replace("..", "").strip()
    if not safe_id.endswith(".json"):
        safe_id = f"{safe_id}.json"
    path_gcs = _history_path_for_user(safe, safe_id, for_gcs=True)
    path_local = _history_path_for_user(safe, safe_id, for_gcs=False)
    if GCS_BUCKET and path_gcs:
        raw = _gcs_read(path_gcs)
        if raw:
            try: return json.loads(raw)
            except Exception: return None
        return None
    if path_local and os.path.isfile(path_local):
        try:
            with open(path_local, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _survey_history_save(email, data):
    """儲存一筆 Survey 歷史。優先 Firestore，否則 GCS/本地。回傳 (history_id, share_path)。"""
    now = datetime.now(timezone.utc)
    history_id = now.strftime("%Y%m%d_%H%M%S")
    addr = (data.get("address_display") or "").strip()
    if addr:
        safe_addr = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', addr)[:30]
        history_id = f"{history_id}_{safe_addr}"
    data["created_at"] = now.isoformat()
    safe = _safe_email(email)

    db = _get_db()
    if db and email:
        try:
            db.collection("users").document(email).collection("survey_history").document(history_id).set(data)
            return history_id, f"{safe}/{history_id}"
        except Exception as e:
            logging.warning("Survey: Firestore 儲存歷史失敗，改用 GCS/本地: %s", e)

    # Fallback
    data_str = json.dumps(data, ensure_ascii=False, indent=1)
    path_gcs = _history_path_for_user(safe, history_id + ".json", for_gcs=True)
    path_local = _history_path_for_user(safe, history_id + ".json", for_gcs=False)
    if GCS_BUCKET and path_gcs:
        _gcs_write(path_gcs, data_str)
    elif path_local:
        _atomic_write(path_local, data_str)
    else:
        return None, None
    return history_id, f"{safe}/{history_id}"


def _survey_history_update(email, history_id, updates):
    """更新一筆 Survey 歷史（部分欄位）。優先 Firestore，否則 GCS/本地。"""
    safe = _safe_email(email)
    db = _get_db()
    if db and email:
        try:
            db.collection("users").document(email).collection("survey_history").document(history_id).update(updates)
            return True
        except Exception as e:
            logging.warning("Survey: Firestore 更新歷史失敗: %s", e)

    # Fallback
    safe_id = os.path.basename(history_id).replace("..", "").strip()
    if not safe_id.endswith(".json"):
        safe_id = f"{safe_id}.json"
    path_gcs = _history_path_for_user(safe, safe_id, for_gcs=True)
    path_local = _history_path_for_user(safe, safe_id, for_gcs=False)
    if GCS_BUCKET and path_gcs:
        raw = _gcs_read(path_gcs)
        if not raw:
            return False
        rec = json.loads(raw)
        rec.update(updates)
        _gcs_write(path_gcs, json.dumps(rec, ensure_ascii=False, indent=1))
    elif path_local and os.path.isfile(path_local):
        with open(path_local, "r", encoding="utf-8") as f:
            rec = json.load(f)
        rec.update(updates)
        _atomic_write(path_local, json.dumps(rec, ensure_ascii=False, indent=1))
    else:
        return False
    return True


def _survey_history_delete(email, history_id):
    """刪除一筆 Survey 歷史。優先 Firestore，否則 GCS/本地。"""
    safe = _safe_email(email)
    db = _get_db()
    if db and email:
        try:
            db.collection("users").document(email).collection("survey_history").document(history_id).delete()
            return True
        except Exception as e:
            logging.warning("Survey: Firestore 刪除歷史失敗: %s", e)

    # Fallback
    safe_id = os.path.basename(history_id).replace("..", "").strip()
    if not safe_id.endswith(".json"):
        safe_id = f"{safe_id}.json"
    path_gcs = _history_path_for_user(safe, safe_id, for_gcs=True)
    path_local = _history_path_for_user(safe, safe_id, for_gcs=False)
    if GCS_BUCKET and path_gcs:
        _gcs_delete(path_gcs)
        return True
    if path_local and os.path.isfile(path_local):
        os.remove(path_local)
        return True
    return False


# ── Survey feedback Firestore helpers ──

def _load_feedback():
    """讀取設施反饋資料。優先 Firestore，否則 GCS/本地。"""
    db = _get_db()
    if db:
        try:
            docs = db.collection("survey_feedback").stream()
            return {doc.id: doc.to_dict() for doc in docs}
        except Exception as e:
            logging.warning("Survey: Firestore 讀取 feedback 失敗: %s", e)

    # Fallback
    if GCS_BUCKET:
        raw = _gcs_read("feedback.json")
        if raw:
            try: return json.loads(raw)
            except Exception: return {}
        return {}
    if os.path.isfile(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_feedback_entry(place_id, entry):
    """儲存單一設施的反饋。優先 Firestore，否則暫存並整體覆寫。"""
    db = _get_db()
    if db:
        try:
            db.collection("survey_feedback").document(place_id).set(entry)
            return True
        except Exception as e:
            logging.warning("Survey: Firestore 儲存 feedback 失敗: %s", e)

    # Fallback：讀取整體 → 修改 → 寫回
    data = _load_feedback()
    data[place_id] = entry
    data_str = json.dumps(data, ensure_ascii=False, indent=2)
    with _feedback_lock:
        if GCS_BUCKET:
            _gcs_write("feedback.json", data_str)
        else:
            _atomic_write(FEEDBACK_FILE, data_str)
    return True


# ── History (查詢歷史) ──

@app.route("/api/history", methods=["GET"])
def api_history_list():
    """列出目前登入使用者的歷史紀錄（僅摘要）。未登入回傳 401。
    後端服務可帶 X-Service-Key header 與 email 查詢參數來存取任意用戶資料。"""
    import hmac as _hmac
    svc_key = request.headers.get("X-Service-Key", "")
    if SERVICE_API_KEY and svc_key and _hmac.compare_digest(svc_key, SERVICE_API_KEY):
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"error": "缺少 email"}), 400
    else:
        email = session.get("user_email")
        if not email:
            return jsonify({"error": "請先登入", "login_required": True}), 401

    items = _survey_history_list(email)
    limit = request.args.get("limit", type=int)
    offset = request.args.get("offset", 0, type=int)
    total = len(items)
    if limit is not None:
        items = items[offset:offset + limit]
    return jsonify({"items": items, "total": total}) if limit is not None else jsonify(items)


@app.route("/api/history/<history_id>", methods=["GET"])
def api_history_load(history_id):
    """載入指定歷史紀錄的完整資料（限登入使用者自己的紀錄）。
    後端服務可帶 X-Service-Key header 與 email 查詢參數存取。"""
    import hmac as _hmac
    svc_key = request.headers.get("X-Service-Key", "")
    if SERVICE_API_KEY and svc_key and _hmac.compare_digest(svc_key, SERVICE_API_KEY):
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"error": "缺少 email"}), 400
    else:
        email = session.get("user_email")
        if not email:
            return jsonify({"error": "請先登入", "login_required": True}), 401

    safe_id = os.path.basename(history_id).replace("..", "").strip().replace(".json", "")
    rec = _survey_history_get(email, safe_id)
    if rec is None:
        return jsonify({"error": "紀錄不存在"}), 404
    return jsonify(rec)


@app.route("/api/history", methods=["POST"])
def api_history_save():
    """儲存一筆查詢結果到歷史（存到目前登入使用者的空間）"""
    email = session.get("user_email")
    if not email:
        return jsonify({"error": "請先登入", "login_required": True}), 401
    data = request.get_json() or {}
    if not data.get("facilities"):
        return jsonify({"error": "缺少資料"}), 400

    history_id, share_path = _survey_history_save(email, data)
    if not history_id:
        return jsonify({"error": "無法寫入歷史"}), 500
    return jsonify({"ok": True, "id": history_id, "share_path": share_path})


@app.route("/api/history/shared/<user_safe>/<path:history_id>", methods=["GET"])
def api_history_shared(user_safe, history_id):
    """公開讀取一筆歷史（供分享連結使用，不需登入）"""
    safe_id = os.path.basename(history_id).replace("..", "").strip().replace(".json", "")
    # 先嘗試 Firestore（需要反查 email）
    db = _get_db()
    if db:
        try:
            email_guess = user_safe.replace("_at_", "@").replace("_", ".")
            doc = db.collection("users").document(email_guess).collection("survey_history").document(safe_id).get()
            if doc.exists:
                data = doc.to_dict()
                data.pop("_id", None)
                return jsonify(data)
        except Exception:
            pass

    # Fallback：GCS / 本地
    prefix = _history_user_prefix(user_safe)
    if not prefix:
        return jsonify({"error": "紀錄不存在"}), 404
    if GCS_BUCKET:
        safe_file = safe_id if safe_id.endswith(".json") else f"{safe_id}.json"
        path = f"{HISTORY_GCS_PREFIX}/{prefix}{safe_file}"
        raw = _gcs_read(path)
        if not raw:
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            return jsonify(json.loads(raw))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    safe_file = safe_id if safe_id.endswith(".json") else f"{safe_id}.json"
    fpath = os.path.join(HISTORY_DIR, "users", user_safe, safe_file)
    if not os.path.isfile(fpath):
        return jsonify({"error": "紀錄不存在"}), 404
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/shared/legacy/<path:history_id>", methods=["GET"])
def api_history_shared_legacy(history_id):
    """公開讀取一筆歷史（舊版分享連結，從原 history/ 路徑讀取）"""
    safe_id = os.path.basename(history_id).replace("..", "").strip()
    if not safe_id.endswith(".json"):
        safe_id = f"{safe_id}.json"
    if GCS_BUCKET:
        raw = _gcs_read(f"{HISTORY_GCS_PREFIX}/{safe_id}")
        if not raw:
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            return jsonify(json.loads(raw))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    fpath = os.path.join(HISTORY_DIR, safe_id)
    if not os.path.isfile(fpath):
        return jsonify({"error": "紀錄不存在"}), 404
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/<history_id>/share-text", methods=["GET"])
def api_history_share_text(history_id):
    """產生可分享的純文字摘要（限登入使用者自己的紀錄）"""
    email = session.get("user_email")
    if not email:
        return jsonify({"error": "請先登入", "login_required": True}), 401
    safe_id = os.path.basename(history_id).replace("..", "").strip().replace(".json", "")
    rec = _survey_history_get(email, safe_id)
    if rec is None:
        return jsonify({"error": "紀錄不存在"}), 404
    return jsonify({"text": rec.get("summary", ""), "address": rec.get("address_display", "")})


@app.route("/api/history/<history_id>", methods=["PATCH"])
def api_history_rename(history_id):
    """修改歷史紀錄的自訂名稱（限登入使用者自己的紀錄）"""
    email = session.get("user_email")
    if not email:
        return jsonify({"error": "請先登入", "login_required": True}), 401
    body = request.get_json() or {}
    new_title = (body.get("title") or "").strip()
    safe_id = os.path.basename(history_id).replace("..", "").strip().replace(".json", "")
    ok = _survey_history_update(email, safe_id, {"custom_title": new_title})
    if not ok:
        return jsonify({"error": "紀錄不存在"}), 404
    return jsonify({"ok": True, "title": new_title})


@app.route("/api/history/<history_id>", methods=["DELETE"])
def api_history_delete(history_id):
    """刪除一筆歷史紀錄（限登入使用者自己的紀錄）"""
    email = session.get("user_email")
    if not email:
        return jsonify({"error": "請先登入", "login_required": True}), 401
    safe_id = os.path.basename(history_id).replace("..", "").strip().replace(".json", "")
    ok = _survey_history_delete(email, safe_id)
    if not ok:
        return jsonify({"error": "紀錄不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/search-gemini", methods=["POST"])
def api_search_gemini():
    """逾時備援：用 Gemini 產生該地點生活機能／嫌惡設施簡要說明。POST body 同 /api/search。"""
    data = request.get_json() or {}
    lat = data.get("lat")
    lng = data.get("lng")
    radius_m = data.get("radius_m") or RADIUS_DEFAULT
    address_display = (data.get("address_display") or "").strip()

    if lat is None or lng is None:
        return jsonify({"error": "請提供 lat + lng（請先在地圖上設定座標）"}), 400
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return jsonify({"error": "經緯度格式錯誤"}), 400

    if not address_display:
        address_display = f"{lat}, {lng}"

    try:
        summary = _gemini_summary(address_display, lat, lng, radius_m)
        return jsonify({
            "address_display": address_display,
            "radius_m": radius_m,
            "source": "gemini",
            "summary": summary,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
