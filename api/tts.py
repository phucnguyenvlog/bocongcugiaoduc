import asyncio
import html as html_lib
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import edge_tts

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PUBLIC_ORIGIN = "https://tao-de-on-tap.vercel.app/"
TMP_CACHE_FILE = "/tmp/pn_shortlinks_cache.json"

SHORT_CODE_MEM_CACHE = {}
URL_TO_CODE_MEM_CACHE = {}

VOICES = [
    {
        "id": "vi-VN-HoaiMyNeural",
        "name": "Hoài My (Nữ • Tiếng Việt chuẩn)",
        "badge": "Chuẩn truyền cảm",
        "gender": "female",
        "locale": "vi-VN",
        "description": "Giọng nữ miền Bắc tự nhiên, rõ chữ, đọc đề thi & văn bản dài cực mượt",
    },
    {
        "id": "vi-VN-NamMinhNeural",
        "name": "Nam Minh (Nam • Tiếng Việt chuẩn)",
        "badge": "Trầm ấm rõ nét",
        "gender": "male",
        "locale": "vi-VN",
        "description": "Giọng nam trầm ấm, dứt khoát, phù hợp đọc giảng bài & thuyết minh",
    },
    {
        "id": "en-US-JennyNeural",
        "name": "Jenny (Nữ • Tiếng Anh Mỹ)",
        "badge": "Tiếng Anh Mỹ",
        "gender": "female",
        "locale": "en-US",
        "description": "Giọng nữ bản xứ Mỹ chuẩn cho đề thi Tiếng Anh",
    },
    {
        "id": "en-US-GuyNeural",
        "name": "Guy (Nam • Tiếng Anh Mỹ)",
        "badge": "Tiếng Anh Mỹ",
        "gender": "male",
        "locale": "en-US",
        "description": "Giọng nam bản xứ Mỹ tự nhiên",
    },
    {
        "id": "en-GB-SoniaNeural",
        "name": "Sonia (Nữ • Tiếng Anh Anh)",
        "badge": "Tiếng Anh Anh",
        "gender": "female",
        "locale": "en-GB",
        "description": "Giọng nữ chuẩn Anh-Anh (British English)",
    },
]

MALE_KEYWORDS = (
    "nam",
    "male",
    "hải đăng",
    "hai dang",
    "minh đức",
    "minh duc",
    "thiện minh",
    "thien minh",
    "quang sơn",
    "quang son",
    "thiền tâm",
    "adam",
    "namminh",
)

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _load_tmp_short_cache():
    try:
        if os.path.isfile(TMP_CACHE_FILE):
            with open(TMP_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(k, str) and isinstance(v, str):
                            SHORT_CODE_MEM_CACHE[k] = v
                            URL_TO_CODE_MEM_CACHE[v] = k
    except Exception:
        pass


def _save_short_cache_pair(code: str, full_url: str):
    SHORT_CODE_MEM_CACHE[code] = full_url
    URL_TO_CODE_MEM_CACHE[full_url] = code
    try:
        if len(SHORT_CODE_MEM_CACHE) > 500:
            oldest_key = next(iter(SHORT_CODE_MEM_CACHE))
            SHORT_CODE_MEM_CACHE.pop(oldest_key, None)
        with open(TMP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(SHORT_CODE_MEM_CACHE, f, ensure_ascii=False)
    except Exception:
        pass


_load_tmp_short_cache()


def resolve_voice(voice_input: str) -> str:
    if not voice_input:
        return "vi-VN-HoaiMyNeural"
    v = str(voice_input).strip()
    if v.endswith("Neural"):
        return v
    low = v.lower()
    if any(k in low for k in MALE_KEYWORDS):
        return "vi-VN-NamMinhNeural"
    return "vi-VN-HoaiMyNeural"


def format_rate(speed) -> str:
    try:
        val = float(speed)
    except (TypeError, ValueError):
        val = 1.0
    pct = int(round((val - 1.0) * 100))
    pct = max(-50, min(100, pct))
    return f"{pct:+d}%"


def clean_text(text: str) -> str:
    text = re.sub(r"\[(cười|hắng giọng|thở dài|nghi[^\]]*|khoảng nghỉ[^\]]*)\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_chunks(text: str, max_len: int = 1600):
    if len(text) <= max_len:
        return [text]
    sentences = re.split(r"(?<=[.!?\n;:])\s+", text)
    chunks = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_len:
            if current:
                chunks.append(current)
                current = ""
            words = sent.split(" ")
            part = ""
            for w in words:
                if len(part) + len(w) + 1 <= max_len:
                    part = f"{part} {w}".strip()
                else:
                    if part:
                        chunks.append(part)
                    part = w
            if part:
                chunks.append(part)
        elif len(current) + len(sent) + 1 <= max_len:
            current = f"{current} {sent}".strip()
        else:
            if current:
                chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


async def synth_one_chunk(chunk_text: str, voice: str, rate: str, sem: asyncio.Semaphore) -> bytes:
    last_err = None
    async with sem:
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(text=chunk_text, voice=voice, rate=rate)
                buf = bytearray()
                async for msg in communicate.stream():
                    if msg["type"] == "audio":
                        buf.extend(msg["data"])
                if buf:
                    return bytes(buf)
                last_err = RuntimeError("Empty audio received")
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"TTS chunk failed: {last_err}")


async def synth_full_text(text: str, voice: str, rate: str) -> bytes:
    chunks = split_chunks(text, max_len=1600)
    sem = asyncio.Semaphore(4)
    tasks = [synth_one_chunk(c, voice, rate, sem) for c in chunks]
    parts = await asyncio.gather(*tasks)
    return b"".join(parts)


def load_index_html():
    for candidate in (
        os.path.join(BASE_DIR, "index.html"),
        os.path.join(os.getcwd(), "index.html"),
    ):
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                return f.read()
    return None


def _normalize_base_origin(raw_origin: str) -> str:
    s = str(raw_origin or "").strip()
    if re.match(r"^https?://", s, flags=re.IGNORECASE) and not re.search(r"//(localhost|127\.0\.0\.1)\b", s, flags=re.IGNORECASE):
        s = s.split("#", 1)[0].split("?", 1)[0].rstrip("/") + "/"
        return s
    return DEFAULT_PUBLIC_ORIGIN


def _fetch_redirect_location(short_endpoint: str, timeout_sec: float = 6.5) -> str:
    req = Request(short_endpoint, headers={"User-Agent": BROWSER_UA}, method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r"https?://[^\"'\s<>]+#(?:e|exam|gdrive)=[^\"'\s<>]+", body)
            if m:
                return html_lib.unescape(m.group(0).strip())
    except HTTPError as http_err:
        if http_err.code in (301, 302, 303, 307, 308):
            loc = (http_err.headers.get("Location") or "").strip()
            if re.match(r"^https?://", loc, flags=re.IGNORECASE):
                return loc
    except Exception:
        pass
    return ""


def _fetch_plain_text(url: str, timeout_sec: float = 6.5) -> str:
    req = Request(url, headers={"User-Agent": BROWSER_UA}, method="GET")
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            txt = resp.read().decode("utf-8", errors="ignore").strip()
            return txt
    except Exception:
        return ""


def expand_short_code_uncached(code: str) -> str:
    clean_code = str(code or "").strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{2,48}$", clean_code):
        return ""

    prefix = clean_code[0].lower()
    slug = clean_code[1:]

    if prefix == "p":
        txt = _fetch_plain_text(f"https://dpaste.com/{slug}.txt")
        if re.match(r"^https?://\S+$", txt, flags=re.IGNORECASE):
            return txt
        return ""

    if prefix == "b":
        txt = _fetch_plain_text(f"https://bytebin.lucko.me/{slug}")
        if re.match(r"^https?://\S+$", txt, flags=re.IGNORECASE):
            return txt
        return ""

    if prefix == "d":
        loc = _fetch_redirect_location(f"https://da.gd/{slug}")
        if loc:
            return loc
        txt = _fetch_plain_text(f"https://da.gd/coshorten/{slug}")
        if re.match(r"^https?://\S+$", txt, flags=re.IGNORECASE):
            return txt
        return ""

    if prefix == "i":
        loc = _fetch_redirect_location(f"https://is.gd/{slug}")
        if loc:
            return loc
        txt = html_lib.unescape(_fetch_plain_text(f"https://is.gd/forward.php?format=simple&shorturl={slug}"))
        if re.match(r"^https?://\S+$", txt, flags=re.IGNORECASE):
            return txt
        return ""

    if prefix == "v":
        loc = _fetch_redirect_location(f"https://v.gd/{slug}")
        if loc:
            return loc
        txt = html_lib.unescape(_fetch_plain_text(f"https://v.gd/forward.php?format=simple&shorturl={slug}"))
        if re.match(r"^https?://\S+$", txt, flags=re.IGNORECASE):
            return txt
        return ""

    if prefix == "c":
        return _fetch_redirect_location(f"https://clck.ru/{slug}")

    if prefix == "t":
        return _fetch_redirect_location(f"https://tinyurl.com/{slug}")

    if prefix == "s":
        return _fetch_redirect_location(f"https://spoo.me/{slug}")

    return ""


def expand_short_code(code: str) -> str:
    clean_code = str(code or "").strip()
    if not clean_code:
        return ""
    if clean_code in SHORT_CODE_MEM_CACHE:
        return SHORT_CODE_MEM_CACHE[clean_code]
    _load_tmp_short_cache()
    if clean_code in SHORT_CODE_MEM_CACHE:
        return SHORT_CODE_MEM_CACHE[clean_code]

    expanded = expand_short_code_uncached(clean_code)
    if expanded:
        _save_short_cache_pair(clean_code, expanded)
    return expanded


def _extract_slug_from_provider_url(short_url: str, expected_host: str) -> str:
    try:
        p = urlparse(short_url.strip())
        if expected_host not in (p.netloc or "").lower():
            return ""
        slug = (p.path or "").strip("/")
        if re.match(r"^[a-zA-Z0-9_-]{2,40}$", slug):
            return slug
    except Exception:
        pass
    return ""


def shorten_long_url(target_url: str, base_origin: str = DEFAULT_PUBLIC_ORIGIN) -> dict:
    url_str = str(target_url or "").strip()
    if not re.match(r"^https?://", url_str, flags=re.IGNORECASE):
        raise ValueError("Đường link cần rút gọn phải bắt đầu bằng http:// hoặc https://")

    origin = _normalize_base_origin(base_origin)

    if url_str in URL_TO_CODE_MEM_CACHE:
        cached_code = URL_TO_CODE_MEM_CACHE[url_str]
        return {"code": cached_code, "short_url": f"{origin}?d={cached_code}"}

    form_headers = {
        "User-Agent": BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }

    def _try_dagd():
        req = Request("https://da.gd/s", data=urlencode({"url": url_str}).encode("utf-8"), headers=form_headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "da.gd")
            return f"d{slug}" if slug else ""

    def _try_isgd():
        req = Request("https://is.gd/create.php", data=urlencode({"format": "simple", "url": url_str}).encode("utf-8"), headers=form_headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "is.gd")
            return f"i{slug}" if slug else ""

    def _try_vgd():
        req = Request("https://v.gd/create.php", data=urlencode({"format": "simple", "url": url_str}).encode("utf-8"), headers=form_headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "v.gd")
            return f"v{slug}" if slug else ""

    def _try_clck():
        req = Request("https://clck.ru/--", data=urlencode({"url": url_str}).encode("utf-8"), headers=form_headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "clck.ru")
            return f"c{slug}" if slug else ""

    def _try_dpaste():
        payload = urlencode({"content": url_str, "syntax": "text", "expiry_days": "365"}).encode("utf-8")
        req = Request("https://dpaste.com/api/v2/", data=payload, headers=form_headers, method="POST")
        with urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "dpaste.com")
            return f"p{slug}" if slug else ""

    def _try_bytebin():
        req = Request(
            "https://bytebin.lucko.me/post",
            data=url_str.encode("utf-8"),
            headers={"User-Agent": BROWSER_UA, "Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            slug = str(data.get("key") or "").strip()
            return f"b{slug}" if re.match(r"^[a-zA-Z0-9_-]{3,40}$", slug) else ""

    def _try_spoo():
        headers = dict(form_headers)
        headers["Accept"] = "application/json"
        req = Request("https://spoo.me/", data=urlencode({"url": url_str}).encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="ignore"))
            slug = _extract_slug_from_provider_url(str(parsed.get("short_url") or ""), "spoo.me")
            return f"s{slug}" if slug else ""

    def _try_tinyurl():
        req = Request("https://tinyurl.com/api-create.php", data=urlencode({"url": url_str}).encode("utf-8"), headers=form_headers, method="POST")
        with urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip().splitlines()[0]
            slug = _extract_slug_from_provider_url(raw, "tinyurl.com")
            return f"t{slug}" if slug else ""

    # Nếu đề rất dài (> 3.400 ký tự), ưu tiên dpaste & bytebin (lưu tới 250KB, không bao giờ lỗi link quá dài)
    if len(url_str) > 3400:
        attempts = [_try_dpaste, _try_bytebin, _try_dagd, _try_clck, _try_tinyurl]
    else:
        attempts = [_try_dagd, _try_isgd, _try_vgd, _try_dpaste, _try_bytebin, _try_clck, _try_spoo, _try_tinyurl]

    fallback_code = ""
    for fn in attempts:
        try:
            code = fn()
            if not code:
                continue
            if not fallback_code:
                fallback_code = code
            # Tự kiểm chứng giải mã ngược từ máy chủ để đảm bảo 100% học sinh mở được trên mọi thiết bị
            verified = expand_short_code_uncached(code)
            if verified == url_str:
                _save_short_cache_pair(code, url_str)
                return {"code": code, "short_url": f"{origin}?d={code}"}
        except Exception:
            continue

    if fallback_code:
        _save_short_cache_pair(fallback_code, url_str)
        return {"code": fallback_code, "short_url": f"{origin}?d={fallback_code}"}

    raise RuntimeError("Các máy chủ rút gọn đang tạm bận, vui lòng bấm Làm mới sau giây lát.")


def _build_safe_zalo_landing_html(target_url: str) -> bytes:
    safe_js_url = json.dumps(target_url)
    html_str = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Phúc nguyên Vlog — Đề Ôn Tập Trực Tuyến</title>
    <meta name="description" content="Bấm vào để mở bài ôn tập trực tuyến ngay trên điện thoại hoặc máy tính.">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="Phúc nguyên Vlog">
    <meta property="og:title" content="Phúc nguyên Vlog — Đề Ôn Tập Trực Tuyến">
    <meta property="og:description" content="Trang làm bài ôn tập trực tuyến miễn phí cho học sinh.">
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; background: #f8fafc; color: #0f172a; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 16px; }}
        .card {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 20px; padding: 24px; max-width: 380px; text-align: center; box-shadow: 0 10px 25px -10px rgba(15,23,42,0.1); }}
        .title {{ font-size: 16px; font-weight: 800; margin-bottom: 6px; }}
        .sub {{ font-size: 13px; color: #64748b; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="title">Đang mở Đề Ôn Tập Trực Tuyến...</div>
        <div class="sub">Vui lòng đợi trong giây lát</div>
    </div>
    <script>
        window.location.replace({safe_js_url});
    </script>
</body>
</html>"""
    return html_str.encode("utf-8")


class handler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, html_bytes: bytes):
        self.send_response(status_code)
        self._cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        action = qs.get("action", [""])[0].strip().lower()
        d_code = qs.get("d", [""])[0].strip()
        if not d_code and path.startswith("/d/"):
            d_code = path.split("/d/", 1)[1].split("/", 1)[0].strip()

        # 1. Giải mã mã rút gọn nội bộ sang đường link đề gốc (JSON cho frontend gọi ngầm)
        if action == "expand" or path.endswith("/expand"):
            if not d_code:
                self._send_json(400, {"error": "Thiếu mã rút gọn d."})
                return
            expanded = expand_short_code(d_code)
            if expanded:
                self._send_json(200, {"code": d_code, "url": expanded})
            else:
                self._send_json(404, {"error": "Không tìm thấy hoặc mã rút gọn đã hết hạn."})
            return

        # 2. Nếu người dùng truy cập trực tiếp https://tao-de-on-tap.vercel.app/d/<code> trên trình duyệt hoặc Zalo
        if d_code and not action:
            expanded = expand_short_code(d_code)
            if expanded:
                self._send_html(200, _build_safe_zalo_landing_html(expanded))
                return
            self._send_html(200, _build_safe_zalo_landing_html(DEFAULT_PUBLIC_ORIGIN))
            return

        # 3. Rút gọn link qua GET
        if action == "shorten" or path in ("/shorten", "/api/tts/shorten") or path.endswith("/shorten"):
            target_url = qs.get("url", [""])[0]
            base_origin = qs.get("base_url", [DEFAULT_PUBLIC_ORIGIN])[0]
            if not target_url:
                self._send_json(400, {"error": "Thiếu tham số url cần rút gọn."})
                return
            try:
                res = shorten_long_url(target_url, base_origin)
                self._send_json(200, res)
            except Exception as exc:
                self._send_json(502, {"error": str(exc)})
            return

        if action == "voices" or path in ("/voices", "/api/tts/voices") or path.endswith("/voices"):
            self._send_json(200, {"voices": VOICES})
            return

        if action == "health" or path in ("/health", "/api/tts"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "model": "Microsoft Neural TTS (Vercel)",
                    "engine": "edge-tts",
                    "voices_count": len(VOICES),
                },
            )
            return

        if path in ("/", "/index.html"):
            html = load_index_html()
            if html is not None:
                self._send_html(200, html)
                return

        self._send_json(404, {"detail": "Not Found"})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query_action = parse_qs(parsed.query).get("action", [""])[0]

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))

            action = str(data.get("action") or query_action or "").strip().lower()
            if action == "expand" or path.endswith("/expand"):
                d_code = str(data.get("d") or data.get("code") or "").strip()
                expanded = expand_short_code(d_code)
                if expanded:
                    self._send_json(200, {"code": d_code, "url": expanded})
                else:
                    self._send_json(404, {"error": "Không tìm thấy đường link gốc."})
                return

            if action == "shorten" or path in ("/shorten", "/api/tts/shorten") or path.endswith("/shorten"):
                target_url = str(data.get("url") or "").strip()
                base_origin = str(data.get("base_url") or DEFAULT_PUBLIC_ORIGIN).strip()
                if not target_url:
                    self._send_json(400, {"error": "Thiếu đường link cần rút gọn."})
                    return
                res = shorten_long_url(target_url, base_origin)
                self._send_json(200, res)
                return

            text = clean_text(str(data.get("text") or ""))
            if not text:
                self._send_json(400, {"error": "Văn bản trống, vui lòng nhập nội dung."})
                return

            voice = resolve_voice(data.get("voice") or data.get("voice_id") or "")
            rate = format_rate(data.get("speed", 1.0))

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                audio_bytes = loop.run_until_complete(synth_full_text(text, voice, rate))
            finally:
                loop.close()

            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Disposition", 'inline; filename="voice.mp3"')
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.end_headers()
            self.wfile.write(audio_bytes)
        except Exception as exc:
            self._send_json(500, {"error": f"Lỗi xử lý: {exc}"})
