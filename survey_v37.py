# -*- coding: utf-8 -*-
"""
地理設施調查 v3.7 核心邏輯
使用 Google Places API (New) Text Search + Geocoding API
支援多把 API Key 自動嘗試
"""

import json
import math
import os
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("survey_v37")

RADIUS_DEFAULT = 350

PLACES_NEW_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

FULL_CATEGORIES = [
    "寺廟", "寺", "宮", "廟", "堂", "觀", "庵", "祠", "教堂",
    "天后宮", "后宮", "媽祖", "福德", "土地公", "王爺", "城隍", "天公",
    "便利商店", "7-11", "全家", "超市", "全聯", "傳統市場",
    "國小", "國中", "高中", "幼兒園", "補習班", "圖書館",
    "餐廳", "小吃", "早餐店", "咖啡廳", "飲料店", "便當", "麵包店",
    "診所", "中醫", "牙醫", "藥局", "醫院", "衛生所",
    "公園", "運動場", "籃球場", "健身房", "游泳池",
    "公車站", "捷運站", "火車站", "停車場", "YouBike", "公共自行車",
    "銀行", "郵局", "ATM",
    "瓦斯", "煤氣", "瓦斯行", "加油站", "殯儀館", "火葬場", "納骨塔", "墓地",
    "垃圾場", "資源回收", "回收場", "廢棄物", "變電所", "高壓電塔",
    "區公所", "公所", "審計室", "聯絡處", "國稅局", "稅捐處", "地政", "戶政", "法院", "警察局", "消防局",
]

NUISANCE_CATEGORIES = {"瓦斯行", "加油站", "殯儀館", "殯葬設施", "墓地", "垃圾場/回收", "變電所", "資源回收"}
RELIGIOUS_CATEGORIES = {"宮廟", "寺院", "廟宇", "教堂", "鸞堂/善堂", "祠堂"}

NUISANCE_KEYWORDS = [
    ("回收", "資源回收"), ("廢棄物", "垃圾場/回收"), ("垃圾", "垃圾場/回收"),
    ("資源回收", "資源回收"), ("廢五金", "垃圾場/回收"),
    ("加油站", "加油站"), ("瓦斯", "瓦斯行"), ("煤氣", "瓦斯行"),
    ("殯儀", "殯儀館"), ("殯葬", "殯葬設施"), ("禮儀公司", "殯葬設施"),
    ("納骨", "殯葬設施"), ("靈骨", "殯葬設施"), ("墓", "墓地"),
    ("變電", "變電所"), ("高壓電", "變電所"),
    ("焚化", "垃圾場/回收"), ("垃圾轉運", "垃圾場/回收"),
]


def _reclassify_by_google_label(category, google_type, name):
    """用 Google 分類顯示名 + 設施名稱做二次判斷，修正分類錯誤"""
    combined = f"{google_type} {name}"
    for keyword, nuisance_cat in NUISANCE_KEYWORDS:
        if keyword in combined and category not in NUISANCE_CATEGORIES:
            return nuisance_cat
    return category

_working_key = None


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


GOOGLE_TYPE_MAP = {
    # 宗教
    "hindu_temple": "宮廟", "place_of_worship": "廟宇", "church": "教堂",
    "mosque": "廟宇", "synagogue": "廟宇",
    # 購物
    "convenience_store": "便利商店", "supermarket": "超市", "grocery_store": "超市",
    "market": "傳統市場",
    # 學校
    "primary_school": "國小", "secondary_school": "國中", "school": "學校",
    "university": "大學", "preschool": "幼兒園",
    "library": "圖書館",
    # 餐飲
    "restaurant": "餐廳", "chinese_restaurant": "餐廳", "japanese_restaurant": "餐廳",
    "korean_restaurant": "餐廳", "seafood_restaurant": "餐廳",
    "meal_delivery": "餐廳", "meal_takeaway": "餐廳",
    "cafe": "咖啡廳", "coffee_shop": "咖啡廳",
    "bakery": "麵包店", "food": "小吃店",
    # 醫療
    "hospital": "醫院", "doctor": "診所", "dentist": "牙醫診所",
    "pharmacy": "藥局", "physiotherapist": "診所", "veterinary_care": "動物醫院",
    # 休閒
    "park": "公園", "stadium": "運動場", "gym": "健身房",
    "swimming_pool": "游泳池",
    "golf_course": "高爾夫球場", "sports_club": "運動俱樂部",
    "sports_complex": "運動中心", "bowling_alley": "保齡球館",
    "amusement_park": "遊樂園", "movie_theater": "電影院",
    "tourist_attraction": "觀光景點", "playground": "遊樂場",
    # 交通
    "bus_station": "公車站", "transit_station": "公車站",
    "subway_station": "捷運站", "train_station": "火車站",
    "light_rail_station": "捷運站",
    "parking": "停車場",
    # 金融
    "bank": "銀行", "atm": "ATM",
    # 郵政
    "post_office": "郵局",
    # 嫌惡
    "gas_station": "加油站", "cemetery": "墓地",
    "funeral_home": "殯儀館",
    # 政府
    "local_government_office": "政府機關", "city_hall": "政府機關",
    "courthouse": "法院", "police": "警察局", "fire_station": "消防局",
    # 住宿
    "lodging": "飯店", "hotel": "飯店",
}

GOOGLE_TYPE_LABEL = {
    "hindu_temple": "印度教寺廟", "place_of_worship": "宗教場所", "church": "教堂",
    "mosque": "清真寺", "synagogue": "猶太教堂",
    "convenience_store": "便利商店", "supermarket": "超市", "grocery_store": "雜貨店",
    "market": "市場",
    "primary_school": "國小", "secondary_school": "國中", "school": "學校",
    "university": "大學", "preschool": "托兒所",
    "library": "圖書館",
    "restaurant": "餐廳", "chinese_restaurant": "中式餐廳", "japanese_restaurant": "日式餐廳",
    "korean_restaurant": "韓式餐廳", "seafood_restaurant": "海鮮餐廳",
    "ramen_restaurant": "拉麵店", "barbecue_restaurant": "燒烤店",
    "steak_house": "牛排館", "sushi_restaurant": "壽司店",
    "thai_restaurant": "泰式餐廳", "vietnamese_restaurant": "越南餐廳",
    "indian_restaurant": "印度餐廳", "italian_restaurant": "義式餐廳",
    "american_restaurant": "美式餐廳", "mexican_restaurant": "墨西哥餐廳",
    "brunch_restaurant": "早午餐", "vegetarian_restaurant": "素食餐廳",
    "vegan_restaurant": "純素餐廳", "fast_food_restaurant": "速食店",
    "ice_cream_shop": "冰淇淋店", "dessert_shop": "甜點店",
    "meal_delivery": "外送餐廳", "meal_takeaway": "外帶餐廳",
    "cafe": "咖啡廳", "coffee_shop": "咖啡店",
    "bakery": "烘焙坊", "food": "小吃",
    "hospital": "醫院", "doctor": "診所", "dentist": "牙科",
    "pharmacy": "藥局", "physiotherapist": "物理治療", "veterinary_care": "動物醫院",
    "park": "公園", "stadium": "體育場", "gym": "健身房",
    "swimming_pool": "游泳池", "playground": "遊樂場",
    "golf_course": "高爾夫球場", "sports_club": "運動俱樂部",
    "sports_complex": "運動中心", "bowling_alley": "保齡球館",
    "amusement_park": "遊樂園", "movie_theater": "電影院",
    "tourist_attraction": "觀光景點",
    "bus_station": "公車站", "transit_station": "轉運站",
    "subway_station": "捷運站", "train_station": "火車站",
    "light_rail_station": "輕軌站", "parking": "停車場",
    "bank": "銀行", "atm": "自動提款機",
    "post_office": "郵局",
    "gas_station": "加油站", "cemetery": "墓地", "funeral_home": "殯儀館",
    "local_government_office": "政府機關", "city_hall": "市政府",
    "courthouse": "法院", "police": "警察局", "fire_station": "消防局",
    "lodging": "住宿", "hotel": "飯店", "motel": "汽車旅館",
    "shopping_mall": "購物中心", "department_store": "百貨公司",
    "book_store": "書店", "clothing_store": "服飾店",
    "electronics_store": "3C 賣場", "furniture_store": "家具店",
    "hardware_store": "五金行", "pet_store": "寵物店",
    "hair_salon": "美髮院", "beauty_salon": "美容院", "spa": "SPA",
    "laundry": "洗衣店", "car_wash": "洗車場", "car_repair": "汽車維修",
    "recycling_center": "回收服務中心", "waste_transfer_station": "垃圾轉運站",
    "junk_dealer": "廢棄物回收", "scrap_metal_dealer": "廢五金回收",
    "electric_substation": "變電所", "high_voltage_tower": "高壓電塔",
}

SKIP_TYPES = {"point_of_interest", "establishment", "political", "geocode",
              "street_address", "route", "locality", "sublocality",
              "administrative_area_level_1", "administrative_area_level_2",
              "country", "postal_code"}

GENERIC_DISPLAY_NAMES = {"服務業", "商店", "商業", "企業", "公司", "機構",
                         "組織", "場所", "地點", "事業"}


def _get_google_type_label(place_types, primary_type=None, primary_type_display=None):
    """取得 Google 分類顯示名稱，忠實呈現 Google 標籤。
    當 Google 給的 primaryTypeDisplayName 太籠統時，
    從 types 陣列找更精確的分類。"""
    is_generic = primary_type_display in GENERIC_DISPLAY_NAMES if primary_type_display else True

    if not is_generic and primary_type_display:
        return primary_type_display

    if place_types:
        for t in place_types:
            if t in SKIP_TYPES:
                continue
            if t in GOOGLE_TYPE_LABEL:
                return GOOGLE_TYPE_LABEL[t]

    if primary_type_display:
        return primary_type_display

    if primary_type and primary_type in GOOGLE_TYPE_LABEL:
        return GOOGLE_TYPE_LABEL[primary_type]
    if primary_type and primary_type not in SKIP_TYPES:
        return primary_type.replace("_", " ")
    if place_types:
        for t in place_types:
            if t not in SKIP_TYPES:
                return t.replace("_", " ")
    return ""


def _classify_by_google_types(place_types, primary_type=None):
    """用 Google 回傳的 types / primaryType 判斷類別，回傳 (類別, 是否為宗教)"""
    all_types = list(place_types or [])
    if primary_type:
        all_types = [primary_type] + [t for t in all_types if t != primary_type]

    if not all_types:
        return None, False

    is_religious = any(t in all_types for t in [
        "hindu_temple", "church", "mosque", "synagogue", "place_of_worship",
    ])

    for gtype in all_types:
        if gtype in GOOGLE_TYPE_MAP:
            return GOOGLE_TYPE_MAP[gtype], is_religious

    if is_religious:
        return "廟宇", True

    return None, is_religious


def normalize_category(search_term, place_types=None, primary_type=None):
    """優先用 Google types / primaryType 判斷類別；無法判定時才用搜尋詞"""
    types_cat, is_religious = _classify_by_google_types(place_types, primary_type)

    if types_cat:
        if is_religious:
            return types_cat
        # types 明確為非宗教 → 用 types 結果（解決「烤堂」被當宗教的問題）
        return types_cat

    # ── Google types 無法判定，改用搜尋詞 ──
    t = search_term

    if any(x in t for x in ["寺", "宮", "廟", "堂", "觀", "庵", "祠", "教堂",
                             "媽祖", "福德", "土地公", "王爺", "城隍", "天公"]):
        if "堂" in t and "教堂" not in t: return "鸞堂/善堂"
        if "祠" in t: return "祠堂"
        if "宮" in t or "媽祖" in t or "天公" in t: return "宮廟"
        if "寺" in t: return "寺院"
        if "教堂" in t: return "教堂"
        if "福德" in t or "土地公" in t: return "廟宇"
        if "王爺" in t or "城隍" in t: return "廟宇"
        return "廟宇"
    if any(x in t for x in ["7-11", "全家", "OK", "萊爾富", "便利商店"]): return "便利商店"
    if any(x in t for x in ["全聯", "家樂福", "超市"]): return "超市"
    if "國小" in t: return "國小"
    if "國中" in t: return "國中"
    if "高中" in t: return "高中"
    if "幼兒園" in t: return "幼兒園"
    if "補習班" in t: return "補習班"
    if "圖書館" in t: return "圖書館"
    if "中醫" in t: return "中醫診所"
    if "牙醫" in t: return "牙醫診所"
    if "診所" in t: return "診所"
    if "藥局" in t or "藥房" in t: return "藥局"
    if "醫院" in t: return "醫院"
    if "衛生所" in t: return "衛生所"
    if "早餐" in t: return "早餐店"
    if "咖啡" in t: return "咖啡廳"
    if "便當" in t: return "便當店"
    if "小吃" in t: return "小吃店"
    if "飲料" in t: return "飲料店"
    if "麵包" in t: return "麵包店"
    if "餐廳" in t: return "餐廳"
    if "瓦斯" in t or "煤氣" in t: return "瓦斯行"
    if "殯儀" in t or "禮儀" in t: return "殯儀館"
    if "火葬" in t or "納骨" in t: return "殯葬設施"
    if "墓地" in t or "公墓" in t: return "墓地"
    if "垃圾" in t or "回收" in t or "廢棄物" in t or "廢五金" in t: return "垃圾場/回收"
    if "變電" in t or "高壓電" in t: return "變電所"
    if "加油站" in t: return "加油站"
    if "區公所" in t or "公所" in t or "政府" in t: return "政府機關"
    if "審計" in t or "主計" in t: return "審計/主計"
    if "聯絡處" in t: return "聯絡處"
    if "地政" in t: return "地政事務所"
    if "戶政" in t: return "戶政事務所"
    if "法院" in t: return "法院"
    if "警察" in t: return "警察局"
    if "消防" in t: return "消防局"
    if "國稅" in t or "稅捐" in t: return "稅務機關"
    if "公園" in t: return "公園"
    if "運動場" in t: return "運動場"
    if "籃球" in t: return "籃球場"
    if "健身房" in t or "健身" in t: return "健身房"
    if "游泳池" in t: return "游泳池"
    if "公車" in t: return "公車站"
    if "捷運" in t: return "捷運站"
    if "火車" in t: return "火車站"
    if "停車" in t: return "停車場"
    if "youbike" in t.lower() or "公共自行車" in t or "ubike" in t.lower(): return "YouBike"
    if "銀行" in t: return "銀行"
    if "郵局" in t: return "郵局"
    if "ATM" in t or "提款" in t: return "ATM"
    return search_term


def _get_all_api_keys():
    """收集所有可能可用的 Google API Key（去重、保留順序）"""
    candidates = []
    for var in [
        "GOOGLE_MAPS_API_KEY",       # 地理調查優先（.env 底部覆蓋的那把）
        "GOOGLE_PLACES_API_KEY",
        "GOOGLE_MAPS_API_KEY_002",
        "GOOGLE_MAPS_API_KEY_001",
        "GOOGLE_API_KEY",
        "GOOGLE_AI_STUDIO_API_KEY",
        "GOOGLE_OPENCLAW_API_KEY",
    ]:
        val = os.environ.get(var)
        if val and val not in candidates:
            candidates.append(val)

    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            k = (data.get("skills") or {}).get("entries", {}).get("goplaces", {}).get("apiKey") or ""
            if k and k not in candidates:
                candidates.append(k)
        except Exception:
            pass

    return candidates


def _get_api_key():
    global _working_key
    if _working_key:
        return _working_key
    keys = _get_all_api_keys()
    if keys:
        log.info("✅ 找到 %d 把候選 API Key", len(keys))
        return keys[0]
    log.error("❌ 無法取得任何 Google API Key！")
    return ""


def _test_places_key(api_key, lat=22.7598, lng=121.1552):
    """測試某把 key 是否能用 Places API (New) Text Search"""
    try:
        resp = requests.post(
            PLACES_NEW_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id",
            },
            json={
                "textQuery": "便利商店",
                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 500.0}},
                "languageCode": "zh-TW",
                "maxResultCount": 1,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.debug("   key %s...：HTTP %d — %s", api_key[:8], resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.debug("   key %s...：錯誤 %s", api_key[:8], e)
        return False


def _find_working_key(lat=22.7598, lng=121.1552):
    """從所有候選 key 中找出第一把能成功呼叫 Places API (New) 的"""
    global _working_key
    if _working_key:
        return _working_key
    keys = _get_all_api_keys()
    log.info("🔑 測試 %d 把 API Key…", len(keys))
    for k in keys:
        log.info("   嘗試 %s…%s", k[:8], k[-4:])
        if _test_places_key(k, lat, lng):
            log.info("   ✅ 可用！")
            _working_key = k
            return k
        log.info("   ❌ 不可用")
    log.error("❌ 所有 API Key 皆無法使用 Places API (New)")
    return ""


def _bounding_box(lat, lng, radius_m):
    """將中心點+半徑轉為矩形 bounding box (south, west, north, east)"""
    delta_lat = radius_m / 111_320
    delta_lng = radius_m / (111_320 * math.cos(math.radians(lat)))
    return (lat - delta_lat, lng - delta_lng, lat + delta_lat, lng + delta_lng)


def query_places(category, lat, lng, radius_m, api_key):
    """用 Places API (New) Text Search 查詢，以 locationRestriction 嚴格限定範圍"""
    if not api_key:
        return []
    search_radius = radius_m * 1.5
    south, west, north, east = _bounding_box(lat, lng, search_radius)
    try:
        resp = requests.post(
            PLACES_NEW_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.primaryType,places.primaryTypeDisplayName",
            },
            json={
                "textQuery": category,
                "locationRestriction": {"rectangle": {
                    "low": {"latitude": south, "longitude": west},
                    "high": {"latitude": north, "longitude": east},
                }},
                "languageCode": "zh-TW",
                "maxResultCount": 20,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("⚠️ [%s] HTTP %d：%s", category, resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        results = data.get("places", [])
        log.debug("   [%s] 取得 %d 筆", category, len(results))
        return results
    except Exception as e:
        log.error("❌ [%s] 查詢錯誤：%s", category, e)
        return []


NEARBY_TYPE_GROUPS = [
    (["church", "hindu_temple", "mosque", "synagogue"], "宗教場所"),
    (["restaurant", "cafe", "bakery", "meal_takeaway", "meal_delivery"], "餐飲"),
    (["convenience_store", "supermarket"], "購物"),
    (["hospital", "doctor", "dentist", "pharmacy"], "醫療"),
    (["school", "primary_school", "secondary_school", "university"], "教育"),
    (["bus_station", "train_station", "subway_station", "transit_station"], "交通"),
    (["park", "gym", "swimming_pool", "stadium", "library"], "休閒"),
    (["bank", "atm", "post_office"], "金融"),
    (["gas_station", "funeral_home", "cemetery"], "嫌惡設施"),
    (["local_government_office", "courthouse", "police", "fire_station"], "政府"),
]


def query_nearby(types, lat, lng, radius_m, api_key):
    """用 Places API (New) Nearby Search 按 Google 地點類型搜尋，補齊 Text Search 的遺漏"""
    if not api_key:
        return []
    try:
        resp = requests.post(
            NEARBY_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.primaryType,places.primaryTypeDisplayName",
            },
            json={
                "includedTypes": types,
                "locationRestriction": {"circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_m * 1.5),
                }},
                "maxResultCount": 20,
                "languageCode": "zh-TW",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("⚠️ [Nearby %s] HTTP %d：%s", types[0], resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        results = data.get("places", [])
        log.debug("   [Nearby %s] 取得 %d 筆", types[0], len(results))
        return results
    except Exception as e:
        log.error("❌ [Nearby %s] 查詢錯誤：%s", types[0], e)
        return []


def resolve_address(address, api_key=None):
    """用 Places API (New) Text Search 解析地址為經緯度（不依賴 Geocoding API）"""
    api_key = api_key or _find_working_key()
    if not api_key:
        api_key = _get_api_key()
    if not api_key:
        return None
    log.debug("📍 解析地址：%s", address)
    try:
        resp = requests.post(
            PLACES_NEW_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.location,places.displayName,places.formattedAddress",
            },
            json={
                "textQuery": address.strip(),
                "languageCode": "zh-TW",
                "maxResultCount": 1,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("⚠️ 地址解析 HTTP %d：%s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        places = data.get("places", [])
        if places:
            loc = places[0].get("location", {})
            lat = loc.get("latitude")
            lng = loc.get("longitude")
            if lat is not None and lng is not None:
                log.info("✅ 地址解析成功：%s → (%s, %s)", address, lat, lng)
                return (float(lat), float(lng))
        log.warning("⚠️ 地址解析無結果")
        return None
    except Exception as e:
        log.error("❌ 地址解析錯誤：%s", e)
        return None


CONVENIENCE_CATS = {"便利商店", "超市", "傳統市場"}
FOOD_CATS = {"餐廳", "小吃店", "早餐店", "咖啡廳", "飲料店", "便當店", "麵包店"}
MEDICAL_CATS = {"診所", "中醫診所", "牙醫診所", "藥局", "醫院", "衛生所", "動物醫院"}
EDUCATION_CATS = {"國小", "國中", "高中", "幼兒園", "補習班", "圖書館", "學校", "大學"}
TRANSPORT_CATS = {"公車站", "捷運站", "火車站", "停車場"}
RECREATION_CATS = {"公園", "運動場", "籃球場", "健身房", "游泳池"}
FINANCE_CATS = {"銀行", "郵局", "ATM"}

# ── 房價利多：知名品牌 / 指標機構 ──
NOTABLE_BRANDS = {
    "7-eleven", "7-11", "統一超商", "全家", "familymart", "ok超商", "萊爾富", "hi-life",
    "全聯", "全聯福利中心", "家樂福", "carrefour", "大潤發", "愛買", "costco", "好市多",
    "星巴克", "starbucks", "麥當勞", "mcdonald", "摩斯漢堡", "mos burger",
    "肯德基", "kfc", "必勝客", "pizza hut", "subway", "漢堡王", "burger king",
    "鼎泰豐", "王品", "瓦城", "爭鮮", "壽司郎",
    "寶雅", "康是美", "屈臣氏", "watsons", "大樹藥局", "丁丁藥局",
    "誠品", "uniqlo", "無印良品", "muji", "迪卡儂", "decathlon",
    "秀泰", "威秀", "國賓",
}
NOTABLE_CATEGORIES = {
    "國小", "國中", "高中", "大學", "學校",
    "幼兒園", "圖書館",
    "醫院",
    "公園",
    "捷運站", "火車站",
    "郵局", "銀行",
    "警察局", "消防局",
}
NOTABLE_GOOGLE_TYPES = {
    "primary_school", "secondary_school", "university", "school",
    "hospital", "park", "library",
    "train_station", "subway_station", "light_rail_station",
    "post_office", "bank",
    "shopping_mall", "department_store",
    "movie_theater",
}


def is_notable(name, category, google_types=None):
    """判斷設施是否為房價利多的指標設施"""
    if category in NOTABLE_CATEGORIES:
        return True
    name_lower = name.lower().strip()
    for brand in NOTABLE_BRANDS:
        if brand in name_lower:
            return True
    if google_types:
        for t in google_types:
            if t in NOTABLE_GOOGLE_TYPES:
                return True
    return False


# ── 距離人性化與分析工具 ──

def dist_to_walk_min(m):
    return max(1, round(m / 80))


def dist_to_bike_min(m):
    return max(1, round(m / 250))


def format_distance_human(m):
    walk = dist_to_walk_min(m)
    if m > 500:
        return f"步行約 {walk} 分鐘（騎車約 {dist_to_bike_min(m)} 分鐘）"
    return f"步行約 {walk} 分鐘"


def get_life_circle(m):
    if m <= 150:
        return "core"
    elif m <= 350:
        return "walking"
    return "extended"


LIFE_CIRCLE_LABELS = {
    "core": "🏠 核心生活圈（150m 內）— 出門就到",
    "walking": "🚶 步行生活圈（150-350m）— 走路可達",
    "extended": "🚲 擴展生活圈（350-500m）— 稍遠但仍方便",
}

ESSENTIAL_NEEDS = [
    ("便利商店/超市", CONVENIENCE_CATS),
    ("藥局", {"藥局"}),
    ("醫療設施", MEDICAL_CATS),
    ("幼兒園", {"幼兒園"}),
    ("公園", {"公園"}),
    ("大眾運輸", {"公車站", "捷運站", "火車站"}),
]


def analyze_missing(facilities, all_found, radius_m):
    missing = []
    for label, cats in ESSENTIAL_NEEDS:
        in_range = [f for f in facilities if f["category"] in cats]
        if not in_range:
            beyond = sorted(
                [f for f in all_found if f["category"] in cats],
                key=lambda x: x["distance"],
            )
            if beyond:
                n = beyond[0]
                missing.append({
                    "type": label, "nearest_name": n["name"],
                    "nearest_dist": round(n["distance"]),
                    "nearest_walk": dist_to_walk_min(n["distance"]),
                })
            else:
                missing.append({
                    "type": label, "nearest_name": None,
                    "nearest_dist": None, "nearest_walk": None,
                })
    return missing


def assess_nuisance_impact(nuisance_list):
    results = []
    for f in nuisance_list:
        d = f["distance"]
        if d <= 100:
            level, emoji, label = "high", "🔴", "高度影響"
            note = "100m 內，可能影響房價 5-10%，建議現場確認是否可從窗戶目視"
        elif d <= 200:
            level, emoji, label = "medium", "🟡", "中度影響"
            note = "200m 內，建議實地確認噪音或氣味影響"
        else:
            level, emoji, label = "low", "🟢", "輕度影響"
            note = "距離較遠，日常影響有限"
        results.append({
            **f, "impact_level": level, "impact_emoji": emoji,
            "impact_label": label, "impact_note": note,
            "walk_min": dist_to_walk_min(d),
        })
    return results


def religious_impact_notes(religious_list):
    notes = []
    temple_cats = {"宮廟", "寺院", "廟宇", "鸞堂/善堂", "祠堂"}
    for f in religious_list:
        d = f["distance"]
        if f["category"] not in temple_cats:
            continue
        if d <= 100:
            notes.append({
                **f, "warning": f"{f['name']} 距離僅 {round(d)}m",
                "details": [
                    "留意農曆初一十五、節慶期間可能有鞭炮及誦經聲",
                    "附近可能有金爐，燒金紙時會有煙味",
                    "建議現場確認噪音與氣味影響",
                ],
            })
        elif d <= 200:
            notes.append({
                **f, "warning": f"{f['name']} 在 {round(d)}m 處",
                "details": ["節慶期間可能聽到鞭炮聲，平時影響較小"],
            })
    return notes


def calculate_weighted_score(facilities, nuisance):
    """拆分為「生活方便性」和「嫌惡設施」雙評分，加上綜合推薦指數"""
    conv_breakdown = []
    conv_total = 0.0

    conv = [f for f in facilities if f["category"] in CONVENIENCE_CATS]
    conv_close = [f for f in conv if f["distance"] <= 150]
    if conv_close:
        pts = 1.2
        conv_breakdown.append({"cat": "購物", "pts": pts, "max": 1.2,
                               "note": f"便利商店/超市 150m 內有 {len(conv_close)} 間（滿分）"})
    elif conv:
        pts = 0.6
        conv_breakdown.append({"cat": "購物", "pts": pts, "max": 1.2,
                               "note": f"有 {len(conv)} 間但距離較遠（半分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "購物", "pts": pts, "max": 1.2, "note": "附近無便利商店或超市"})
    conv_total += pts

    med = [f for f in facilities if f["category"] in MEDICAL_CATS]
    if med:
        pts = 0.8
        conv_breakdown.append({"cat": "醫療", "pts": pts, "max": 0.8,
                               "note": f"有 {len(med)} 間醫療設施（滿分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "醫療", "pts": pts, "max": 0.8, "note": "附近無醫療設施"})
    conv_total += pts

    edu = [f for f in facilities if f["category"] in EDUCATION_CATS]
    if edu:
        pts = 0.6
        conv_breakdown.append({"cat": "教育", "pts": pts, "max": 0.6,
                               "note": f"有 {len(edu)} 間學校/教育機構（滿分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "教育", "pts": pts, "max": 0.6, "note": "附近無學校或教育機構"})
    conv_total += pts

    transit_cats = {"公車站", "捷運站", "火車站"}
    trans = [f for f in facilities if f["category"] in transit_cats]
    trans_close = [f for f in trans if f["distance"] <= 350]
    if trans_close:
        pts = 1.0
        conv_breakdown.append({"cat": "交通", "pts": pts, "max": 1.0,
                               "note": f"350m 內有 {len(trans_close)} 個站點（滿分）"})
    elif trans:
        pts = 0.5
        conv_breakdown.append({"cat": "交通", "pts": pts, "max": 1.0,
                               "note": f"有 {len(trans)} 個站點但距離稍遠（半分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "交通", "pts": pts, "max": 1.0, "note": "附近無大眾運輸站點"})
    conv_total += pts

    food = [f for f in facilities if f["category"] in FOOD_CATS]
    food_types = set(f["category"] for f in food)
    if len(food_types) >= 3:
        pts = 0.6
        conv_breakdown.append({"cat": "餐飲", "pts": pts, "max": 0.6,
                               "note": f"餐飲種類豐富（{len(food)} 間、{len(food_types)} 類）（滿分）"})
    elif food_types:
        pts = 0.3
        conv_breakdown.append({"cat": "餐飲", "pts": pts, "max": 0.6,
                               "note": f"有 {len(food)} 間但種類較少（半分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "餐飲", "pts": pts, "max": 0.6, "note": "附近無餐飲設施"})
    conv_total += pts

    park = [f for f in facilities if f["category"] == "公園"]
    if park:
        pts = 0.4
        conv_breakdown.append({"cat": "休閒", "pts": pts, "max": 0.4,
                               "note": f"有 {len(park)} 座公園（滿分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "休閒", "pts": pts, "max": 0.4, "note": "附近無公園"})
    conv_total += pts

    fin = [f for f in facilities if f["category"] in FINANCE_CATS]
    if fin:
        pts = 0.3
        conv_breakdown.append({"cat": "金融", "pts": pts, "max": 0.3,
                               "note": f"有 {len(fin)} 間金融服務（滿分）"})
    else:
        pts = 0.0
        conv_breakdown.append({"cat": "金融", "pts": pts, "max": 0.3, "note": "附近無銀行/ATM/郵局"})
    conv_total += pts

    conv_max = 4.9
    convenience_score = max(0, min(5, round(conv_total / conv_max * 5, 1)))

    # ── 嫌惡設施評分（5 = 無嫌惡，0 = 嚴重）──
    nui_breakdown = []
    nui_penalty = 0.0
    for nf in nuisance:
        d = nf["distance"]
        if d <= 100:
            p = 1.5
            nui_breakdown.append({"cat": nf.get("category", ""), "name": nf["name"],
                                  "pts": -p, "note": f"{round(d)}m — 高度影響"})
        elif d <= 200:
            p = 1.0
            nui_breakdown.append({"cat": nf.get("category", ""), "name": nf["name"],
                                  "pts": -p, "note": f"{round(d)}m — 中度影響"})
        else:
            p = 0.5
            nui_breakdown.append({"cat": nf.get("category", ""), "name": nf["name"],
                                  "pts": -p, "note": f"{round(d)}m — 輕度影響"})
        nui_penalty += p
    nuisance_score = max(0, min(5, round(5 - nui_penalty, 1)))
    nui_warning_count = min(5, max(0, round(5 - nuisance_score)))
    nui_display = "⚠️" * nui_warning_count if nui_warning_count > 0 else "✅"

    # ── 綜合推薦指數（1-5 👍）──
    raw_recommend = convenience_score * 0.65 + nuisance_score * 0.35
    recommend_thumbs = max(1, min(5, round(raw_recommend)))

    # ── 生活方便性敘述 ──
    strengths = [b for b in conv_breakdown if b["pts"] > 0 and b["pts"] >= b["max"] * 0.8]
    weaknesses = [b for b in conv_breakdown if b["pts"] == 0 and b["max"] > 0]
    partial = [b for b in conv_breakdown if 0 < b["pts"] < b["max"] * 0.8]
    parts = []
    if strengths:
        parts.append(f"{'、'.join(b['cat'] for b in strengths)}表現不錯")
    if weaknesses:
        parts.append(f"但{'、'.join(b['cat'] for b in weaknesses)}缺乏")
    if partial:
        parts.append(f"{'、'.join(b['cat'] for b in partial)}有但不夠完善")

    if convenience_score >= 4:
        conv_verdict = "生活非常便利，日常所需一應俱全"
    elif convenience_score >= 3:
        conv_verdict = "生活便利性良好，多數需求步行可達"
    elif convenience_score >= 2:
        conv_verdict = "基本生活尚可，部分需求需要外出較遠"
    elif convenience_score >= 1:
        conv_verdict = "生活機能偏弱，日常採買較不方便"
    else:
        conv_verdict = "生活機能嚴重不足"
    conv_narrative = "，".join(parts) + f"，{conv_verdict}。" if parts else f"{conv_verdict}。"

    # ── 嫌惡設施敘述 ──
    if not nuisance:
        nui_narrative = "範圍內無嫌惡設施，居住環境單純。"
    elif nuisance_score >= 4:
        nui_narrative = f"有 {len(nuisance)} 處嫌惡設施但距離較遠，影響有限。"
    elif nuisance_score >= 2.5:
        nui_narrative = f"有 {len(nuisance)} 處嫌惡設施，建議現場確認影響程度。"
    else:
        nui_narrative = f"有 {len(nuisance)} 處嫌惡設施且距離很近，可能明顯影響居住品質和房價。"

    # ── 推薦敘述 ──
    thumbs_display = "👍" * recommend_thumbs
    if recommend_thumbs >= 4:
        rec_narrative = f"綜合推薦指數 {thumbs_display}（{recommend_thumbs}/5）— 值得推薦，生活便利且環境良好。"
    elif recommend_thumbs >= 3:
        rec_narrative = f"綜合推薦指數 {thumbs_display}（{recommend_thumbs}/5）— 整體不錯，可安心入住。"
    elif recommend_thumbs >= 2:
        rec_narrative = f"綜合推薦指數 {thumbs_display}（{recommend_thumbs}/5）— 尚可，建議實地考察後決定。"
    else:
        rec_narrative = f"綜合推薦指數 {thumbs_display}（{recommend_thumbs}/5）— 需謹慎評估，建議多方比較。"

    return {
        "convenience_score": convenience_score,
        "nuisance_score": nuisance_score,
        "nui_warning_count": nui_warning_count,
        "nui_display": nui_display,
        "recommend_thumbs": recommend_thumbs,
        "score": round(raw_recommend, 1),
        "convenience_breakdown": conv_breakdown,
        "nuisance_breakdown": nui_breakdown,
        "conv_narrative": conv_narrative,
        "nui_narrative": nui_narrative,
        "rec_narrative": rec_narrative,
        "thumbs_display": thumbs_display,
    }


def generate_persona_summaries(facilities, missing_analysis):
    def find_nearest(cats):
        for f in facilities:
            if f["category"] in cats:
                return f
        return None

    personas = []

    items = []
    for cat, label in [("國小", "國小"), ("幼兒園", "幼兒園"), ("公園", "公園"), ("藥局", "藥局")]:
        f = find_nearest({cat})
        if f:
            items.append(f"{label} {dist_to_walk_min(f['distance'])} 分 ✅")
    for m in missing_analysis:
        if m["type"] in {"幼兒園", "公園", "藥局"} and not m["nearest_name"]:
            items.append(f"缺{m['type']} ⚠️")
        elif m["type"] in {"幼兒園", "公園", "藥局"} and m["nearest_name"]:
            items.append(f"缺{m['type']}（最近 {m['nearest_walk']} 分外）⚠️")
    if items:
        personas.append({"emoji": "👨‍👩‍👧", "label": "小家庭看這裡", "items": items})

    items = []
    for cats, label in [({"診所", "中醫診所"}, "診所"), ({"醫院"}, "醫院"),
                        ({"傳統市場"}, "市場"), ({"藥局"}, "藥局"), ({"公園"}, "公園")]:
        f = find_nearest(cats)
        if f:
            items.append(f"{label} {dist_to_walk_min(f['distance'])} 分 ✅")
    if items:
        personas.append({"emoji": "👴", "label": "長輩看這裡", "items": items})

    items = []
    for cats, label in [({"火車站"}, "火車站"), ({"捷運站"}, "捷運站"),
                        ({"公車站"}, "公車站"), ({"便利商店"}, "便利商店"), ({"咖啡廳"}, "咖啡廳")]:
        f = find_nearest(cats)
        if f:
            items.append(f"{label} {dist_to_walk_min(f['distance'])} 分 ✅")
    if items:
        personas.append({"emoji": "🧑‍💼", "label": "上班族看這裡", "items": items})

    return personas


def generate_summary(facilities, stats, score, nuisance, religious, address_display, radius_m,
                     nuisance_impact=None, religious_notes=None, missing=None, personas=None,
                     score_result=None):
    """產生結構化總結報告（可餵給廣告詞/影音腳本系統）"""
    def all_in(cats):
        return [f for f in facilities if f["category"] in cats]

    lines = []
    lines.append(f"📍 {address_display or '地點'} 周邊生活機能報告")
    lines.append(f"調查範圍：{radius_m}m｜共 {len(facilities)} 處設施")
    lines.append("")

    if score_result:
        cs = score_result["convenience_score"]
        ns = score_result["nuisance_score"]
        thumbs = score_result["thumbs_display"]
        rt = score_result["recommend_thumbs"]

        lines.append(f"🏪 生活方便性：{'★' * int(round(cs))}{'☆' * (5 - int(round(cs)))}（{cs}/5）")
        lines.append(f"  {score_result['conv_narrative']}")
        lines.append("")

        lines.append("📊 方便性明細：")
        for b in score_result["convenience_breakdown"]:
            if b["pts"] > 0:
                bar = "▓" * int(round(b["pts"] / b["max"] * 5)) if b["max"] > 0 else ""
                lines.append(f"  {b['cat']}：+{b['pts']} / {b['max']}　{bar}　{b['note']}")
            else:
                lines.append(f"  {b['cat']}：0 / {b['max']}　✗ {b['note']}")
        lines.append("")

        nui_disp = score_result.get("nui_display", "✅")
        if score_result["nuisance_breakdown"]:
            lines.append(f"{nui_disp} 嫌惡設施：{score_result.get('nui_warning_count', 0)} 級警示")
            lines.append(f"  {score_result['nui_narrative']}")
            for nb in score_result["nuisance_breakdown"]:
                lines.append(f"  • {nb['name']}（{nb['cat']}）{nb['note']}（{nb['pts']}）")
        else:
            lines.append(f"✅ 嫌惡設施：無 — 環境單純")
        lines.append("")

        lines.append(f"{thumbs} 綜合推薦指數：{rt}/5")
        lines.append(f"  {score_result['rec_narrative']}")
        lines.append("")

    # ── 房價利多指標 ──
    notable_list = [f for f in facilities if f.get("notable") and f["category"] not in NUISANCE_CATEGORIES]
    if notable_list:
        lines.append("⭐ 房價利多指標設施：")
        for f in notable_list[:12]:
            lines.append(f"  ⭐ {f['name']}（{f['category']}）— {format_distance_human(f['distance'])}")
        if len(notable_list) > 12:
            lines.append(f"  …等共 {len(notable_list)} 處")
        lines.append("")

    # ── 生活圈概覽 ──
    def _name_with_star(f):
        star = "⭐" if f.get("notable") else ""
        return f"{star}{f['name']}（{f['category']}）"

    core = [f for f in facilities if f["distance"] <= 150 and f["category"] not in NUISANCE_CATEGORIES]
    walking = [f for f in facilities if 150 < f["distance"] <= 350 and f["category"] not in NUISANCE_CATEGORIES]
    if core:
        lines.append("🏠 核心生活圈（150m 內，出門就到）：")
        names = "、".join(_name_with_star(f) for f in core[:8])
        lines.append(f"  {names}")
        if len(core) > 8:
            lines.append(f"  …等共 {len(core)} 處")
        lines.append("")
    if walking:
        lines.append("🚶 步行生活圈（150-350m，走路可達）：")
        names = "、".join(_name_with_star(f) for f in walking[:8])
        lines.append(f"  {names}")
        if len(walking) > 8:
            lines.append(f"  …等共 {len(walking)} 處")
        lines.append("")

    # ── 生活機能亮點 ──
    highlights = []
    conv = all_in(CONVENIENCE_CATS)
    if conv:
        names = "、".join(f["name"] for f in conv[:3])
        highlights.append(f"購物便利：{len(conv)} 間（{names}），最近{format_distance_human(conv[0]['distance'])}")
    food = all_in(FOOD_CATS)
    if food:
        names = "、".join(f["name"] for f in food[:3])
        highlights.append(f"餐飲豐富：{len(food)} 間（{names}），最近{format_distance_human(food[0]['distance'])}")
    med = all_in(MEDICAL_CATS)
    if med:
        names = "、".join(f["name"] for f in med[:3])
        highlights.append(f"醫療便利：{len(med)} 間（{names}），最近{format_distance_human(med[0]['distance'])}")
    edu = all_in(EDUCATION_CATS)
    if edu:
        names = "、".join(f["name"] for f in edu[:3])
        highlights.append(f"學區資源：{len(edu)} 間（{names}），最近{format_distance_human(edu[0]['distance'])}")
    trans = all_in(TRANSPORT_CATS)
    if trans:
        names = "、".join(f["name"] for f in trans[:3])
        highlights.append(f"交通便捷：{len(trans)} 個（{names}），最近{format_distance_human(trans[0]['distance'])}")
    rec = all_in(RECREATION_CATS)
    if rec:
        names = "、".join(f["name"] for f in rec[:3])
        highlights.append(f"休閒運動：{len(rec)} 處（{names}），最近{format_distance_human(rec[0]['distance'])}")
    fin = all_in(FINANCE_CATS)
    if fin:
        names = "、".join(f["name"] for f in fin[:3])
        highlights.append(f"金融服務：{len(fin)} 處（{names}），最近{format_distance_human(fin[0]['distance'])}")
    if highlights:
        lines.append("✅ 生活機能亮點：")
        for h in highlights:
            lines.append(f"  • {h}")
        lines.append("")

    # ── 缺少的重要設施 ──
    if missing:
        lines.append(f"⚠️ 本區域 {radius_m}m 內未找到：")
        for m in missing:
            if m["nearest_name"]:
                lines.append(
                    f"  • {m['type']}（最近的「{m['nearest_name']}」"
                    f"在 {m['nearest_dist']}m 外，{format_distance_human(m['nearest_dist'])}）"
                )
            else:
                lines.append(f"  • {m['type']}（搜尋範圍內均未找到）")
        lines.append("")

    # ── 嫌惡設施 ──
    ni = nuisance_impact or []
    if ni:
        lines.append("⚠️ 嫌惡設施提醒：")
        for n in ni:
            lines.append(f"  {n['impact_emoji']} {n['name']}（{n['category']}）"
                         f"{round(n['distance'])}m — {n['impact_label']}")
            lines.append(f"    └ {n['impact_note']}")
        lines.append("")
    elif not nuisance:
        lines.append(f"✅ 無嫌惡設施（{radius_m}m 內無加油站、殯葬、變電所等）")
        lines.append("")

    # ── 宗教設施影響提示 ──
    rn = religious_notes or []
    if rn:
        lines.append("🛕 宗教設施影響提示：")
        for r in rn:
            lines.append(f"  • {r['warning']}")
            for d in r.get("details", []):
                lines.append(f"    └ {d}")
        lines.append("")
    elif religious:
        lines.append(f"🛕 宗教設施：共 {len(religious)} 處（距離較遠，影響有限）")
        lines.append("")

    # ── 族群速覽 ──
    if personas:
        lines.append("👥 不同族群速覽：")
        for p in personas:
            items_str = "、".join(p["items"])
            lines.append(f"  {p['emoji']} {p['label']}：{items_str}")
        lines.append("")

    # ── 特別提醒 ──
    reminders = []
    for n in ni:
        if n["impact_level"] == "high":
            reminders.append(f"{round(n['distance'])}m 內有{n['category']}（{n['name']}），建議現場確認是否影響視野")
    for r in rn:
        if r["distance"] <= 100:
            reminders.append(f"{round(r['distance'])}m 內有宮廟（{r['name']}），留意節慶噪音")
    if reminders:
        lines.append("📌 特別提醒：")
        for rem in reminders:
            lines.append(f"  • {rem}")
        lines.append("")

    # ── 一句話摘要 ──
    tag_parts = []
    if score >= 4:
        tag_parts.append("生活機能優良")
    elif score >= 3:
        tag_parts.append("生活機能良好")
    elif score >= 2:
        tag_parts.append("生活機能尚可")
    if conv:
        tag_parts.append("購物便利")
    if food and len(food) >= 3:
        tag_parts.append("美食豐富")
    if edu:
        tag_parts.append("學區優勢")
    if trans:
        tag_parts.append("交通便捷")
    if med:
        tag_parts.append("醫療完善")
    if not nuisance:
        tag_parts.append("無嫌惡設施")

    tagline = "、".join(tag_parts[:5]) if tag_parts else "環境單純"
    lines.append(f"📝 一句話：{tagline}")
    if tag_parts:
        lines.append(f"#標籤：{'  #'.join(tag_parts[:6])}")

    return "\n".join(lines)


def verify_completeness(facilities):
    warnings = []
    religious = [f for f in facilities if f["category"] in RELIGIOUS_CATEGORIES]
    if len(religious) == 0:
        warnings.append("未找到任何宗教設施，市區常有宮廟，可能遺漏")
    elif len(religious) == 1:
        warnings.append("僅找到 1 座宗教設施，台灣市區通常有更多，建議手動補查或標註可能遺漏")
    if not any(f["category"] == "便利商店" for f in facilities):
        warnings.append("未找到便利商店，可能有遺漏")
    return warnings


def run_survey(lat, lng, radius_m=None, api_key=None):
    """執行 v3.7 完整調查"""
    radius_m = radius_m or RADIUS_DEFAULT

    api_key = api_key or _find_working_key(lat, lng)
    if not api_key:
        raise RuntimeError(
            "所有 API Key 皆無法使用 Google Places API。"
            "請到 Google Cloud Console 啟用 Places API (New)，"
            "或確認 .env 裡的 GOOGLE_MAPS_API_KEY / GOOGLE_PLACES_API_KEY 已啟用該 API。"
        )

    all_facilities = {}
    total = len(FULL_CATEGORIES)
    log.info("🚀 開始調查：(%s, %s) 半徑 %dm，共 %d 個類別", lat, lng, radius_m, total)

    for i, category in enumerate(FULL_CATEGORIES, 1):
        log.info("📦 [%d/%d] 查詢：%s", i, total, category)
        items = query_places(category, lat, lng, radius_m, api_key)
        added = 0
        for item in items:
            place_id = item.get("id")
            if not place_id:
                continue
            loc = item.get("location", {})
            plat = loc.get("latitude")
            plng = loc.get("longitude")
            if plat is None or plng is None:
                continue
            dist = haversine_m(lat, lng, plat, plng)
            if dist <= radius_m * 1.5 and place_id not in all_facilities:
                name = (item.get("displayName") or {}).get("text", "")
                ptypes = item.get("types", [])
                ptype = item.get("primaryType", "")
                ptype_disp = (item.get("primaryTypeDisplayName") or {}).get("text", "")
                cat = normalize_category(category, ptypes, ptype)
                gtype_label = _get_google_type_label(ptypes, ptype, ptype_disp)
                cat = _reclassify_by_google_label(cat, gtype_label, name)
                log.debug("   📌 %s → types=%s, primaryType=%s, dispName=%s → label=%s, cat=%s",
                          name, ptypes, ptype, ptype_disp, gtype_label, cat)
                all_facilities[place_id] = {
                    "name": name,
                    "distance": round(dist, 1),
                    "category": cat,
                    "address": item.get("formattedAddress", ""),
                    "lat": plat,
                    "lng": plng,
                    "place_id": place_id,
                    "google_type": gtype_label,
                    "notable": is_notable(name, cat, ptypes),
                }
                added += 1
        if added > 0:
            log.info("   ✅ 新增 %d 筆（累計 %d）", added, len(all_facilities))

    # ── 第二階段：Nearby Search 按 Google 地點類型補齊 ──
    log.info("🔄 第二階段：Nearby Search 補齊（共 %d 組類型）", len(NEARBY_TYPE_GROUPS))
    for idx, (types, group_name) in enumerate(NEARBY_TYPE_GROUPS, 1):
        log.info("📦 [Nearby %d/%d] %s：%s", idx, len(NEARBY_TYPE_GROUPS), group_name, types)
        items = query_nearby(types, lat, lng, radius_m, api_key)
        added = 0
        for item in items:
            place_id = item.get("id")
            if not place_id or place_id in all_facilities:
                continue
            loc = item.get("location", {})
            plat = loc.get("latitude")
            plng = loc.get("longitude")
            if plat is None or plng is None:
                continue
            dist = haversine_m(lat, lng, plat, plng)
            if dist <= radius_m * 1.5:
                name = (item.get("displayName") or {}).get("text", "")
                ptypes = item.get("types", [])
                ptype = item.get("primaryType", "")
                ptype_disp = (item.get("primaryTypeDisplayName") or {}).get("text", "")
                cat = normalize_category(group_name, ptypes, ptype)
                gtype_label = _get_google_type_label(ptypes, ptype, ptype_disp)
                cat = _reclassify_by_google_label(cat, gtype_label, name)
                log.debug("   📌 %s → types=%s, primaryType=%s, dispName=%s → label=%s, cat=%s",
                          name, ptypes, ptype, ptype_disp, gtype_label, cat)
                all_facilities[place_id] = {
                    "name": name,
                    "distance": round(dist, 1),
                    "category": cat,
                    "address": item.get("formattedAddress", ""),
                    "lat": plat,
                    "lng": plng,
                    "place_id": place_id,
                    "google_type": gtype_label,
                    "notable": is_notable(name, cat, ptypes),
                }
                added += 1
        if added > 0:
            log.info("   ✅ 補齊 %d 筆（累計 %d）", added, len(all_facilities))

    all_found = sorted(all_facilities.values(), key=lambda x: x["distance"])
    facilities = [f for f in all_found if f["distance"] <= radius_m]

    for f in facilities:
        f["walk_min"] = dist_to_walk_min(f["distance"])
        f["life_circle"] = get_life_circle(f["distance"])
    for f in all_found:
        if "walk_min" not in f:
            f["walk_min"] = dist_to_walk_min(f["distance"])

    stats = {}
    for f in facilities:
        stats[f["category"]] = stats.get(f["category"], 0) + 1

    nuisance_raw = [f for f in facilities if f["category"] in NUISANCE_CATEGORIES]
    religious = [f for f in facilities if f["category"] in RELIGIOUS_CATEGORIES]
    life_facilities = [f for f in facilities if f["category"] not in NUISANCE_CATEGORIES]

    score_result = calculate_weighted_score(facilities, nuisance_raw)
    score = score_result["score"]
    stars = "⭐" * int(round(score)) + "☆" * (5 - int(round(score)))
    nuisance_impact = assess_nuisance_impact(nuisance_raw)
    rel_notes = religious_impact_notes(religious)
    missing = analyze_missing(facilities, all_found, radius_m)
    personas = generate_persona_summaries(facilities, missing)

    summary = generate_summary(
        facilities, stats, score, nuisance_raw, religious, None, radius_m,
        nuisance_impact=nuisance_impact, religious_notes=rel_notes,
        missing=missing, personas=personas,
        score_result=score_result,
    )

    return {
        "lat": lat, "lng": lng, "radius_m": radius_m, "address_display": None,
        "facilities": facilities, "stats": stats,
        "score": score, "stars": stars,
        "convenience_score": score_result["convenience_score"],
        "nuisance_score": score_result["nuisance_score"],
        "recommend_thumbs": score_result["recommend_thumbs"],
        "thumbs_display": score_result["thumbs_display"],
        "score_result": score_result,
        "nuisance": nuisance_raw, "nuisance_impact": nuisance_impact,
        "religious": religious, "religious_notes": rel_notes,
        "life_facilities": life_facilities,
        "missing": missing, "personas": personas,
        "warnings": verify_completeness(facilities),
        "generated_at": datetime.now().isoformat(),
        "total_count": len(facilities),
        "summary": summary,
    }
