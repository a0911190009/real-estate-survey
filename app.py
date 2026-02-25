# -*- coding: utf-8 -*-
"""
地理設施調查 v3.7 網頁後端
輸入：座標 (lat, lng) 或地址 → 依 v3.7 規則查詢範圍內生活機能與嫌惡設施
逾時時可改用 Gemini 取得簡要說明（需 GOOGLE_API_KEY 或 GOOGLE_AI_STUDIO_API_KEY）
"""

import os
import json
from flask import Flask, request, jsonify, send_from_directory

try:
    from dotenv import load_dotenv
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _local_env = os.path.join(_app_dir, ".env")
    # 優先：ENV_FILE 指定路徑
    _env_file = os.environ.get("ENV_FILE")
    if _env_file and os.path.isfile(_env_file):
        load_dotenv(_env_file, override=False)
    # 其次：本專案目錄的 .env（你放的 .env 會被讀到）
    elif os.path.isfile(_local_env):
        load_dotenv(_local_env, override=False)
    # 最後：若尚未有 Gemini key，再試外部路徑
    elif not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_API_KEY")):
        _external = "/Users/bagel/real_estate_app/.env"
        if os.path.isfile(_external):
            load_dotenv(_external, override=False)
except Exception:
    pass

from survey_v37 import run_survey, resolve_address, RADIUS_DEFAULT

app = Flask(__name__, static_folder="static", static_url_path="")


@app.route("/api/config")
def api_config():
    """回傳前端所需的公開設定（目前只有 Maps API Key 供地圖圖層使用）"""
    from survey_v37 import _get_all_api_keys, _find_working_key, _working_key
    key = _working_key
    if not key:
        key = _find_working_key()
    return jsonify({"maps_api_key": key or ""})


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
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
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


def _load_feedback():
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


def _save_feedback(data):
    data_str = json.dumps(data, ensure_ascii=False, indent=2)
    if GCS_BUCKET:
        _gcs_write("feedback.json", data_str)
    else:
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            f.write(data_str)


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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/view/<history_id>")
def view_shared(history_id):
    """共享連結：載入歷史紀錄並直接顯示結果"""
    return send_from_directory("static", "index.html")


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

    if action == "reset":
        fb.pop(place_id, None)
    elif action == "comment":
        entry = fb.get(place_id, {})
        entry["name"] = data.get("name", entry.get("name", ""))
        entry["comment"] = data.get("comment", "")
        entry["updated_at"] = datetime.now().isoformat()
        fb[place_id] = entry
    elif action == "severity":
        entry = fb.get(place_id, {})
        entry["name"] = data.get("name", entry.get("name", ""))
        entry["severity_override"] = data.get("severity", "")
        entry["updated_at"] = datetime.now().isoformat()
        fb[place_id] = entry
    else:
        fb[place_id] = {
            "action": action,
            "name": data.get("name", ""),
            "nuisance_cat": data.get("nuisance_cat", "嫌惡設施(自訂)"),
            "updated_at": datetime.now().isoformat(),
        }
    _save_feedback(fb)
    return jsonify({"ok": True, "total_feedback": len(fb)})


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
        with open(GENERAL_FEEDBACK_FILE, "w", encoding="utf-8") as f:
            f.write(data_str)
    return jsonify({"ok": True, "total": len(entries)})


@app.route("/api/resolve-address", methods=["POST"])
def api_resolve_address():
    """POST { "address": "台東市中山路123號" } → { "lat": ..., "lng": ... }"""
    data = request.get_json() or {}
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "請提供 address"}), 400
    coords = resolve_address(address)
    if coords:
        return jsonify({"lat": coords[0], "lng": coords[1]})
    return jsonify({"error": "無法解析地址"}), 400


@app.route("/api/search", methods=["POST"])
def api_search():
    """POST body: { "lat": 22.76, "lng": 121.15 }，可選 "radius_m": 350, "address_display": "..." """
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


# ── History (查詢歷史) ──

@app.route("/api/history", methods=["GET"])
def api_history_list():
    """列出所有歷史紀錄（僅摘要，不含完整設施資料）"""
    items = []
    if GCS_BUCKET:
        blobs = sorted(_gcs_list("history/"), reverse=True)
        for bname in blobs:
            if not bname.endswith(".json"):
                continue
            try:
                raw = _gcs_read(bname)
                if not raw:
                    continue
                rec = json.loads(raw)
                fid = bname.replace("history/", "").replace(".json", "")
                items.append({
                    "id": fid,
                    "address": rec.get("address_display", ""),
                    "lat": rec.get("lat"),
                    "lng": rec.get("lng"),
                    "radius_m": rec.get("radius_m"),
                    "score": rec.get("score"),
                    "convenience_score": rec.get("convenience_score"),
                    "recommend_thumbs": rec.get("recommend_thumbs"),
                    "thumbs_display": rec.get("thumbs_display"),
                    "total_count": rec.get("total_count"),
                    "created_at": rec.get("created_at", ""),
                })
            except Exception:
                continue
    else:
        for fname in sorted(os.listdir(HISTORY_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(HISTORY_DIR, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                items.append({
                    "id": fname.replace(".json", ""),
                    "address": rec.get("address_display", ""),
                    "lat": rec.get("lat"),
                    "lng": rec.get("lng"),
                    "radius_m": rec.get("radius_m"),
                    "score": rec.get("score"),
                    "convenience_score": rec.get("convenience_score"),
                    "recommend_thumbs": rec.get("recommend_thumbs"),
                    "thumbs_display": rec.get("thumbs_display"),
                    "total_count": rec.get("total_count"),
                    "created_at": rec.get("created_at", ""),
                })
            except Exception:
                continue
    return jsonify(items)


@app.route("/api/history/<history_id>", methods=["GET"])
def api_history_load(history_id):
    """載入指定歷史紀錄的完整資料"""
    safe_id = os.path.basename(history_id)
    if GCS_BUCKET:
        raw = _gcs_read(f"history/{safe_id}.json")
        if not raw:
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            return jsonify(json.loads(raw))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        fpath = os.path.join(HISTORY_DIR, f"{safe_id}.json")
        if not os.path.isfile(fpath):
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/history", methods=["POST"])
def api_history_save():
    """儲存一筆查詢結果到歷史"""
    data = request.get_json() or {}
    if not data.get("facilities"):
        return jsonify({"error": "缺少資料"}), 400

    from datetime import datetime
    import re
    now = datetime.now()
    history_id = now.strftime("%Y%m%d_%H%M%S")
    addr = (data.get("address_display") or "").strip()
    if addr:
        safe_addr = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', addr)[:30]
        history_id = f"{history_id}_{safe_addr}"

    data["created_at"] = now.isoformat()
    data_str = json.dumps(data, ensure_ascii=False, indent=1)
    if GCS_BUCKET:
        _gcs_write(f"history/{history_id}.json", data_str)
    else:
        fpath = os.path.join(HISTORY_DIR, f"{history_id}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(data_str)
    return jsonify({"ok": True, "id": history_id})


@app.route("/api/history/<history_id>/share-text", methods=["GET"])
def api_history_share_text(history_id):
    """產生可分享的純文字摘要"""
    safe_id = os.path.basename(history_id)
    if GCS_BUCKET:
        raw = _gcs_read(f"history/{safe_id}.json")
        if not raw:
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            rec = json.loads(raw)
            return jsonify({"text": rec.get("summary", ""), "address": rec.get("address_display", "")})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        fpath = os.path.join(HISTORY_DIR, f"{safe_id}.json")
        if not os.path.isfile(fpath):
            return jsonify({"error": "紀錄不存在"}), 404
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                rec = json.load(f)
            return jsonify({"text": rec.get("summary", ""), "address": rec.get("address_display", "")})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/history/<history_id>", methods=["DELETE"])
def api_history_delete(history_id):
    """刪除一筆歷史紀錄"""
    safe_id = os.path.basename(history_id)
    if GCS_BUCKET:
        _gcs_delete(f"history/{safe_id}.json")
        return jsonify({"ok": True})
    else:
        fpath = os.path.join(HISTORY_DIR, f"{safe_id}.json")
        if os.path.isfile(fpath):
            os.remove(fpath)
            return jsonify({"ok": True})
        return jsonify({"error": "紀錄不存在"}), 404


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
