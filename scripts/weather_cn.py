#!/usr/bin/env python3
"""中国天气查询脚本.

功能:
- 接收明确参数: 位置描述
- 默认展示未来 7 日天气
- 通过高德地理编码查询坐标 (支持本地缓存)
- 通过彩云天气按坐标查询天气 (支持本地缓存)
- 聚合展示未来几日 + 小时预报
- 支持 basic/full 两种详情级别与 text/json 输出
- 提供 --mock 离线调试模式
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


SKYCON_MAP = {
    "CLEAR_DAY": "晴",
    "CLEAR_NIGHT": "晴夜",
    "PARTLY_CLOUDY_DAY": "多云",
    "PARTLY_CLOUDY_NIGHT": "多云夜",
    "CLOUDY": "阴",
    "LIGHT_HAZE": "轻度雾霾",
    "MODERATE_HAZE": "中度雾霾",
    "HEAVY_HAZE": "重度雾霾",
    "LIGHT_RAIN": "小雨",
    "MODERATE_RAIN": "中雨",
    "HEAVY_RAIN": "大雨",
    "STORM_RAIN": "暴雨",
    "FOG": "雾",
    "LIGHT_SNOW": "小雪",
    "MODERATE_SNOW": "中雪",
    "HEAVY_SNOW": "大雪",
    "STORM_SNOW": "暴雪",
    "DUST": "浮尘",
    "SAND": "沙尘",
    "WIND": "大风",
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DEFAULT_DAYS = 7


class JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = Path(f"{path}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, ttl_seconds: int) -> Optional[Any]:
        data = self._safe_load()
        item = data.get(key)
        if not item:
            return None
        ts = item.get("ts", 0)
        if time.time() - ts > ttl_seconds:
            return None
        return item.get("value")

    def set(self, key: str, value: Any) -> None:
        with self.lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            data = self._safe_load()
            data[key] = {"ts": time.time(), "value": value}
            self._atomic_write(data)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _safe_load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return obj
            return {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _atomic_write(self, obj: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as tmp:
            json.dump(obj, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.replace(temp_name, self.path)


def normalize_place(place: str) -> str:
    place = re.sub(r"\s+", "", place)
    place = place.rstrip("，。,.;；：:、")
    place = re.sub(r"的$", "", place)
    return place


def load_local_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                key = k.strip()
                value = v.strip().strip("'").strip('"')
                if key:
                    os.environ[key] = value
    except OSError:
        return


def mask_url_for_log(url: str) -> str:
    # 屏蔽高德 key
    masked = re.sub(r"([?&]key=)[^&]+", r"\1***", url)
    # 屏蔽彩云 token
    masked = re.sub(r"/v2(?:\.\d+)?/[^/]+/", "/v2.6/***/", masked)
    return masked


def fetch_json(url: str, timeout: int, retries: int, debug: bool = False) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if debug:
                print(f"[debug] GET {mask_url_for_log(url)}")
            req = urllib.request.Request(url, headers={"User-Agent": "weather-cn-skill-debug/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
            return json.loads(payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                sleep_sec = 0.6 * (2**attempt)
                if debug:
                    print(f"[debug] request failed ({exc}), retry in {sleep_sec:.1f}s")
                time.sleep(sleep_sec)
                continue
            break
    raise RuntimeError(f"请求失败: {last_error}")


def geocode_by_amap(place: str, amap_key: str, timeout: int, retries: int, debug: bool) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"address": place, "key": amap_key})
    url = f"https://restapi.amap.com/v3/geocode/geo?{query}"
    data = fetch_json(url, timeout=timeout, retries=retries, debug=debug)

    if str(data.get("status")) != "1":
        raise RuntimeError(f"高德接口返回失败: {data.get('info') or data}")
    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise RuntimeError(f"未找到地名坐标: {place}")

    first = geocodes[0]
    location = first.get("location") or ""
    try:
        lng_raw, lat_raw = location.split(",", 1)
        lng = float(lng_raw)
        lat = float(lat_raw)
    except ValueError as exc:
        raise RuntimeError(f"高德返回坐标格式异常: {location}") from exc

    return {
        "query_place": place,
        "resolved_address": first.get("formatted_address") or place,
        "lng": lng,
        "lat": lat,
        "province": first.get("province"),
        "city": first.get("city"),
        "district": first.get("district"),
        "adcode": first.get("adcode"),
    }


def build_mock_weather(lng: float, lat: float, days: int, detail: str, hourly_steps: int) -> Dict[str, Any]:
    base_temp = 14.0 + (abs(lat) % 5)
    temp_daily = []
    sky_daily = []
    date_base = datetime.now().date()
    sky_cycle = ["CLEAR_DAY", "PARTLY_CLOUDY_DAY", "LIGHT_RAIN", "CLOUDY", "MODERATE_RAIN"]
    for i in range(days):
        day = date_base.fromordinal(date_base.toordinal() + i)
        min_temp = round(base_temp - 4 + (i % 2), 1)
        max_temp = round(base_temp + 3 + (i % 3), 1)
        temp_daily.append({"date": day.isoformat(), "min": min_temp, "max": max_temp})
        sky_daily.append({"date": day.isoformat(), "value": sky_cycle[i % len(sky_cycle)]})
    result: Dict[str, Any] = {
        "status": "ok",
        "result": {
            "realtime": {
                "temperature": round(base_temp + 0.8, 1),
                "apparent_temperature": round(base_temp + 0.2, 1),
                "skycon": "PARTLY_CLOUDY_DAY",
                "humidity": 0.62,
                "wind": {"speed": 12.0, "direction": 85},
                "air_quality": {"aqi": {"chn": 58, "usa": 46}, "pm25": 16},
            },
            "daily": {
                "temperature": temp_daily,
                "skycon": sky_daily,
            },
        },
        "location": [lng, lat],
    }
    effective_hourly_steps = max(1, min(hourly_steps, 48))
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    hourly_temp = []
    hourly_sky = []
    hourly_precip = []
    for i in range(effective_hourly_steps):
        dt = now.fromtimestamp(now.timestamp() + i * 3600).isoformat(timespec="minutes")
        precip_value = round((i % 5) * 0.03, 2)
        hourly_temp.append({"datetime": dt, "value": round(base_temp + (i % 4) * 0.6, 1)})
        hourly_sky.append({"datetime": dt, "value": sky_cycle[i % len(sky_cycle)]})
        hourly_precip.append(
            {"datetime": dt, "value": precip_value, "probability": int(round(precip_value * 100))}
        )

    result["result"]["hourly"] = {
        "temperature": hourly_temp,
        "skycon": hourly_sky,
        "precipitation": hourly_precip,
    }

    if detail == "full":
        minutely_prob = [round((i % 8) * 0.05, 2) for i in range(120)]
        result["result"]["minutely"] = {
            "description": "未来两小时有零星小雨",
            "probability": minutely_prob,
        }
        result["result"]["daily"]["life_index"] = {
            "ultraviolet": [{"date": date_base.isoformat(), "index": "2", "desc": "弱"}],
            "carWashing": [{"date": date_base.isoformat(), "index": "2", "desc": "较适宜"}],
            "dressing": [{"date": date_base.isoformat(), "index": "3", "desc": "较舒适"}],
        }
        result["result"]["alert"] = {
            "content": [
                {
                    "title": "雷电黄色预警",
                    "code": "11B02",
                    "status": "预警中",
                    "description": "局地可能伴随雷电活动。",
                    "pubtimestamp": int(time.time()),
                }
            ]
        }
    return result


def weather_by_caiyun(
    lng: float,
    lat: float,
    days: int,
    detail: str,
    hourly_steps: int,
    token: str,
    timeout: int,
    retries: int,
    debug: bool,
    mock: bool,
) -> Dict[str, Any]:
    if mock:
        return build_mock_weather(lng=lng, lat=lat, days=days, detail=detail, hourly_steps=hourly_steps)

    if not token:
        raise RuntimeError("缺少 CAIYUN_API_TOKEN")

    token_safe = urllib.parse.quote(token, safe="")
    query_params: Dict[str, Any] = {"dailysteps": days, "alert": "true", "hourlysteps": hourly_steps}
    params = urllib.parse.urlencode(query_params)
    url = f"https://api.caiyunapp.com/v2.6/{token_safe}/{lng},{lat}/weather.json?{params}"
    data = fetch_json(url, timeout=timeout, retries=retries, debug=debug)
    if data.get("status") != "ok":
        raise RuntimeError(f"彩云接口返回失败: {data.get('status')} {data.get('error') or ''}".strip())
    return data


def skycon_cn(code: Optional[str]) -> str:
    if not code:
        return "未知"
    return SKYCON_MAP.get(code, code)


def normalize_date(date_text: str) -> str:
    if "T" in date_text:
        return date_text.split("T", 1)[0]
    return date_text


def normalize_datetime(dt_text: str) -> str:
    if not dt_text:
        return dt_text
    text = dt_text.replace("T", " ")
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", text)
    if m:
        return m.group(1)
    return text


def normalize_probability_percent(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)):
        return None
    if value <= 1:
        return round(float(value) * 100, 1)
    return round(float(value), 1)


def parse_date_safe(date_text: str) -> Optional[date]:
    if not date_text:
        return None
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None


def day_weekday_text(date_text: str) -> str:
    dt = parse_date_safe(date_text)
    if not dt:
        return ""
    return WEEKDAY_CN[dt.weekday()]


def extract_realtime(weather_data: Dict[str, Any]) -> Dict[str, Any]:
    realtime = ((weather_data.get("result") or {}).get("realtime") or {})
    air_quality = realtime.get("air_quality") if isinstance(realtime, dict) else {}
    aqi = air_quality.get("aqi") if isinstance(air_quality, dict) else {}
    humidity = realtime.get("humidity") if isinstance(realtime, dict) else None
    humidity_percent = None
    if isinstance(humidity, (int, float)):
        humidity_percent = round(humidity * 100)

    return {
        "temperature": realtime.get("temperature"),
        "apparent_temperature": realtime.get("apparent_temperature"),
        "skycon": skycon_cn(realtime.get("skycon")),
        "humidity_percent": humidity_percent,
        "wind_speed": ((realtime.get("wind") or {}).get("speed") if isinstance(realtime, dict) else None),
        "wind_direction": ((realtime.get("wind") or {}).get("direction") if isinstance(realtime, dict) else None),
        "aqi_chn": (aqi.get("chn") if isinstance(aqi, dict) else None),
        "pm25": (air_quality.get("pm25") if isinstance(air_quality, dict) else None),
    }


def extract_minutely_summary(weather_data: Dict[str, Any]) -> Dict[str, Any]:
    minutely = ((weather_data.get("result") or {}).get("minutely") or {})
    probs = minutely.get("probability") or []
    numeric_probs = [p for p in probs if isinstance(p, (int, float))]
    max_prob = round(max(numeric_probs), 3) if numeric_probs else None
    return {
        "description": minutely.get("description"),
        "max_probability": max_prob,
    }


def extract_hourly_forecast(weather_data: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    hourly = ((weather_data.get("result") or {}).get("hourly") or {})
    temps = hourly.get("temperature") or []
    sky = hourly.get("skycon") or []
    precip = hourly.get("precipitation") or []
    result: List[Dict[str, Any]] = []
    n = min(limit, len(temps))
    for i in range(n):
        t = temps[i] if isinstance(temps[i], dict) else {}
        s = sky[i] if i < len(sky) and isinstance(sky[i], dict) else {}
        p = precip[i] if i < len(precip) and isinstance(precip[i], dict) else {}
        result.append(
            {
                "datetime": normalize_datetime(
                    t.get("datetime") or s.get("datetime") or p.get("datetime") or f"H+{i}"
                ),
                "temperature": t.get("value"),
                "skycon": skycon_cn(s.get("value")),
                "precipitation": p.get("value"),
                "precipitation_probability": normalize_probability_percent(p.get("probability")),
            }
        )
    return result


def extract_alerts(weather_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    result_part = weather_data.get("result") or {}
    alert_root = result_part.get("alert") if isinstance(result_part, dict) else None
    if not alert_root:
        alert_root = weather_data.get("alert") or {}
    content = alert_root.get("content") if isinstance(alert_root, dict) else []
    if not isinstance(content, list):
        return []
    alerts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        alerts.append(
            {
                "title": item.get("title"),
                "code": item.get("code"),
                "status": item.get("status"),
                "description": item.get("description") or item.get("desc"),
                "pubtimestamp": item.get("pubtimestamp"),
            }
        )
    return alerts


def extract_life_index_summary(weather_data: Dict[str, Any]) -> Dict[str, Any]:
    daily = ((weather_data.get("result") or {}).get("daily") or {})
    life_index = daily.get("life_index") or {}
    if not isinstance(life_index, dict):
        return {}

    summary: Dict[str, Any] = {}
    for key, values in life_index.items():
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                summary[key] = first.get("desc") or first.get("index") or first.get("value")
            else:
                summary[key] = first
    return summary


def extract_daily_forecast(weather_data: Dict[str, Any], days: int) -> List[Dict[str, Any]]:
    daily = ((weather_data.get("result") or {}).get("daily") or {})
    temps = daily.get("temperature") or []
    sky = daily.get("skycon") or []
    result = []
    limit = min(days, len(temps))
    for i in range(limit):
        t = temps[i] if isinstance(temps[i], dict) else {}
        s = sky[i] if i < len(sky) and isinstance(sky[i], dict) else {}
        result.append(
            {
                "date": normalize_date((t.get("date") or s.get("date") or f"D+{i}")),
                "min": t.get("min"),
                "max": t.get("max"),
                "skycon": skycon_cn(s.get("value")),
            }
        )
    return result


def build_output_payload(
    place: str,
    days: int,
    detail: str,
    geo: Dict[str, Any],
    weather_data: Dict[str, Any],
    include_raw: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "query_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "place": place,
        "resolved_address": geo.get("resolved_address") or place,
        "coord": {"lng": geo.get("lng"), "lat": geo.get("lat")},
        "days": days,
        "realtime": extract_realtime(weather_data),
        "daily": extract_daily_forecast(weather_data, max(days, 1)),
    }
    hourly_limit = 24 if detail == "full" else 6
    hourly = extract_hourly_forecast(weather_data, limit=hourly_limit)
    if hourly:
        payload["hourly"] = hourly

    if detail == "full":
        payload["minutely"] = extract_minutely_summary(weather_data)
        alerts = extract_alerts(weather_data)
        if alerts:
            payload["alerts"] = alerts
        life_index = extract_life_index_summary(weather_data)
        if life_index:
            payload["life_index"] = life_index

    if include_raw:
        payload["raw"] = weather_data
    return payload


def print_output(
    place: str,
    days: int,
    detail: str,
    output_format: str,
    include_raw: bool,
    geo: Dict[str, Any],
    weather_data: Dict[str, Any],
) -> None:
    payload = build_output_payload(
        place=place,
        days=days,
        detail=detail,
        geo=geo,
        weather_data=weather_data,
        include_raw=include_raw,
    )
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    # Telegram-friendly text format: bold titles + bullets + code block
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    resolved = geo.get("resolved_address") or place

    print(f"**{resolved}｜天气**")
    print(f"`查询时间 {now_str}`")
    print("")

    realtime = payload.get("realtime") or {}
    daily = payload.get("daily") or []

    # Keep the message compact by default (TG-friendly)
    daily_show = daily[:3]
    day_title = f"近 {len(daily_show)} 日" if len(daily_show) else "近几日"
    print(f"**{day_title}**")
    for day in daily_show:
        day_date = day.get("date", "")
        weekday = day_weekday_text(day_date)
        weekday_text = weekday if weekday else day_date
        print(
            f"• {weekday_text} {day.get('skycon')}  "
            f"{day.get('min', '--')}～{day.get('max', '--')}°C"
        )
    print("")

    if realtime:
        temp = realtime.get("temperature", "--")
        feel = realtime.get("apparent_temperature", "--")
        hum = realtime.get("humidity_percent", "--")
        print("**当前**")
        print(f"• {temp}°C（体感 {feel}°C）｜湿度 {hum}%")
        print("")

    hourly = payload.get("hourly") or []
    if hourly:
        print("**未来 6 小时**")
        print("```text")
        for item in hourly[:6]:
            # datetime like: 2026-02-26 10:00 → 10:00
            dt_text = item.get("datetime") or ""
            hhmm = dt_text[-5:] if re.match(r".*\d{2}:\d{2}$", dt_text) else dt_text

            sky = item.get("skycon") or "--"
            tval = item.get("temperature")
            t_text = f"{tval:.2f}°C" if isinstance(tval, (int, float)) else f"{tval}°C"

            p_prob = item.get("precipitation_probability")
            p_amount = item.get("precipitation")
            p_prob_text = f"{int(round(p_prob))}%" if isinstance(p_prob, (int, float)) else "--"
            p_amount_text = f"{p_amount:.2f}" if isinstance(p_amount, (int, float)) else "--"

            # Align for readability in monospace
            print(f"{hhmm:>5}  {sky:<2}  {t_text:<8}  降水 {p_prob_text:>3}  {p_amount_text} mm/h")
        print("```")

    if detail == "full":
        if realtime.get("aqi_chn") is not None or realtime.get("pm25") is not None:
            print(f"空气质量: AQI(国标) {realtime.get('aqi_chn', '--')}, PM2.5 {realtime.get('pm25', '--')}")
        minutely = payload.get("minutely") or {}
        if minutely.get("description") or minutely.get("max_probability") is not None:
            max_prob = minutely.get("max_probability")
            max_prob_text = f"{round(max_prob * 100)}%" if isinstance(max_prob, (int, float)) else "--"
            print(f"分钟级降雨: {minutely.get('description', '无')} (最大概率 {max_prob_text})")
        alerts = payload.get("alerts") or []
        if alerts:
            print(f"⚠️ 天气预警: {len(alerts)} 条")
            for item in alerts[:3]:
                print(f"  {item.get('title')} ({item.get('status', '未知状态')})")


def main() -> int:
    load_local_dotenv(Path(".env"))

    parser = argparse.ArgumentParser(
        description="中国天气查询调试脚本（高德地理编码 + 彩云天气）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("place", help="位置描述，例: 北京市朝阳区")
    parser.add_argument("--cache-dir", default="cache", help="缓存目录 (默认: ./cache)")
    parser.add_argument("--geo-ttl-hours", type=int, default=24 * 30, help="地名坐标缓存小时数")
    parser.add_argument("--weather-ttl-minutes", type=int, default=10, help="天气缓存分钟数")
    parser.add_argument("--timeout", type=int, default=8, help="HTTP 超时时间(秒)")
    parser.add_argument("--retries", type=int, default=2, help="HTTP 重试次数")
    parser.add_argument("--amap-key", default=os.getenv("AMAP_API_KEY", ""), help="高德 API Key")
    parser.add_argument("--caiyun-token", default=os.getenv("CAIYUN_API_TOKEN", ""), help="彩云 API Token")
    parser.add_argument("--detail", choices=["basic", "full"], default="basic", help="输出详情级别")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    parser.add_argument("--hourly-steps", type=int, default=24, help="小时级步数 1-360 (full 模式生效)")
    parser.add_argument("--raw-caiyun", action="store_true", help="json 输出时附加原始彩云响应")
    parser.add_argument("--mock", action="store_true", help="离线调试模式，不发起外网请求")
    parser.add_argument("--debug", action="store_true", help="打印调试日志")

    args = parser.parse_args()

    place = normalize_place(args.place)
    days = DEFAULT_DAYS

    if not place:
        raise SystemExit("place 不能为空")
    if days < 1 or days > 15:
        raise SystemExit(f"内部配置错误: DEFAULT_DAYS={days} 超出 1~15")
    if args.hourly_steps < 1 or args.hourly_steps > 360:
        raise SystemExit("hourly-steps 必须在 1~360 之间")
    effective_hourly_steps = args.hourly_steps if args.detail == "full" else 6

    if not args.mock and not args.amap_key:
        raise SystemExit("缺少高德 Key，请设置 AMAP_API_KEY 或 --amap-key")
    if not args.mock and not args.caiyun_token:
        raise SystemExit("缺少彩云 Token，请设置 CAIYUN_API_TOKEN 或 --caiyun-token")

    cache_dir = Path(args.cache_dir)
    geo_cache = JsonCache(cache_dir / "geocode.json")
    weather_cache = JsonCache(cache_dir / "weather.json")
    cache_namespace = "mock" if args.mock else "live"

    geo_key = f"{cache_namespace}:amap:{place}"
    geo_ttl_seconds = args.geo_ttl_hours * 3600
    weather_ttl_seconds = args.weather_ttl_minutes * 60

    geo = geo_cache.get(geo_key, geo_ttl_seconds)
    if geo:
        if args.debug:
            print(f"[debug] geocode cache hit: {geo_key}")
    else:
        if args.debug:
            print(f"[debug] geocode cache miss: {geo_key}")
        if args.mock:
            geo = {
                "query_place": place,
                "resolved_address": place,
                "lng": 116.397428,
                "lat": 39.90923,
            }
        else:
            geo = geocode_by_amap(
                place=place,
                amap_key=args.amap_key,
                timeout=args.timeout,
                retries=args.retries,
                debug=args.debug,
            )
        geo_cache.set(geo_key, geo)

    weather_key = f"{cache_namespace}:caiyun:{geo['lng']:.6f},{geo['lat']:.6f}:d{days}"
    weather_key = f"{weather_key}:detail{args.detail}:h{effective_hourly_steps}"
    weather = weather_cache.get(weather_key, weather_ttl_seconds)
    if weather:
        if args.debug:
            print(f"[debug] weather cache hit: {weather_key}")
    else:
        if args.debug:
            print(f"[debug] weather cache miss: {weather_key}")
        weather = weather_by_caiyun(
            lng=float(geo["lng"]),
            lat=float(geo["lat"]),
            days=days,
            detail=args.detail,
            hourly_steps=effective_hourly_steps,
            token=args.caiyun_token,
            timeout=args.timeout,
            retries=args.retries,
            debug=args.debug,
            mock=args.mock,
        )
        weather_cache.set(weather_key, weather)

    print_output(
        place=place,
        days=days,
        detail=args.detail,
        output_format=args.format,
        include_raw=args.raw_caiyun,
        geo=geo,
        weather_data=weather,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
