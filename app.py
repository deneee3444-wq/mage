import os
import re
import base64
import time
import json
import random
import requests
import threading
import uuid
import urllib.parse
import functools
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

app = Flask(__name__)
app.secret_key = 'mage_studio_local_secret_2024'

# ── Uygulama Şifresi ───────────────────────────────────────
APP_PASSWORD = '123'

# ── Login Decorator ────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.method != 'GET' and not request.path.startswith('/login'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# ── Ayarlar ────────────────────────────────────────────────
FIREBASE_API_KEY = "AIzaSyAzUV2NNUOlLTL04jwmUw9oLhjteuv6Qr4"
CONTINUE_URL     = "https://www.mage.space/explore?onboarding=1"
SCOPES           = ["https://www.googleapis.com/auth/gmail.readonly"]

FIREBASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.mage.space",
    "x-client-version": "Chrome/JsCore/10.14.1/FirebaseCore-web",
    "x-firebase-gmpid": "1:816167389238:web:a5e9b7798fccb4ca517097",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

MAGE_HEADERS_BASE = {
    "accept": "text/x-component",
    "accept-language": "tr-TR,tr;q=0.9",
    "content-type": "text/plain;charset=UTF-8",
    "origin": "https://www.mage.space",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

MODELS = {
    "mango-v2":       {"model_id": "mango-v2",       "architecture": "mango",          "resolution": "2K"},
    "mango-v3":       {"model_id": "mango-v3",       "architecture": "mango",          "resolution": "2K"},
    "mango":          {"model_id": "mango",           "architecture": "mango",          "resolution": "2K"},
    "guava":          {"model_id": "guava",           "architecture": "guava",          "resolution": "1K"},
    "nano_banana_v2": {"model_id": "nano_banana_v2",  "architecture": "nano_banana_v2", "resolution": "2K"},
}

VIDEO_MODELS = {
    "peach_max": {
        "model_id": "peach_max",
        "architecture": "peach_max",
        "aspect_ratio": "cinema",
        "peach_max_aspect_ratio": "16:9",
        "resolution": "480p",
    },
    "kiwi": {
        "model_id": "kiwi",
        "architecture": "kiwi",
        "aspect_ratio": "landscape",
        "kiwi_aspect_ratio": "16:9",
        "resolution": "480p",
    },
}

tasks = {}
task_lock = threading.Lock()
saved_prompts = {}
prompts_lock = threading.Lock()
gallery_items = []
gallery_lock = threading.Lock()

def update_task_state(task_id, updates):
    with task_lock:
        if task_id in tasks:
            tasks[task_id].update(updates)

def log_task(task_id, message):
    with task_lock:
        if task_id in tasks:
            print(f"[{task_id[:8]}] {message}")
            tasks[task_id]['logs'].append(message)

def generate_random_email(base_email="stevecraftstory@gmail.com"):
    name, domain = base_email.split('@')
    result = name[0]
    for char in name[1:]:
        if random.choice([True, False]) and result[-1] != '.':
            result += '.'
        result += char
    suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=5))
    return f"{result}+{suffix}@{domain}"

def _router_state_tree(oob_code):
    page = f'/explore?onboarding=1&apiKey={FIREBASE_API_KEY}&oobCode={oob_code}&mode=signIn&lang=en'
    page_key = f'__PAGE__?{{"onboarding":"1","apiKey":"{FIREBASE_API_KEY}","oobCode":"{oob_code}","mode":"signIn","lang":"en"}}'
    tree = ["", {"children": ["explore", {"children": [page_key, {}, page, "refresh"]}]}, None, None, True]
    return urllib.parse.quote(json.dumps(tree), safe='')

def _settings_router_state_tree():
    tree = ["", {"children": ["settings", {"children": ["__PAGE__", {}, "/settings", "refresh"]}]}, None, None, True]
    return urllib.parse.quote(json.dumps(tree), safe='')

def _explore_router_state_tree():
    tree = ["", {"children": ["explore", {"children": ["__PAGE__", {}, "/explore", "refresh"]}]}, None, None, True]
    return urllib.parse.quote(json.dumps(tree), safe='')

def _creations_router_state_tree():
    tree = ["", {"children": ["creations", {"children": ["__PAGE__", {}, "/creations", "refresh"]}]}, None, None, True]
    return urllib.parse.quote(json.dumps(tree), safe='')

def _parse_cdn_url(resp_text):
    for satir in resp_text.splitlines():
        if satir.startswith("1:"):
            deger = satir[2:].strip().strip('"')
            if deger.startswith("http"):
                return deger
    m = re.search(r'"(https://cdn3\.mage\.space/uploads/[^"]+)"', resp_text)
    if m:
        return m.group(1)
    return None

def _file_to_data_uri(f):
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    image_bytes = f.read()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    ext = os.path.splitext(f.filename)[1].lower()
    mime = mime_map.get(ext, "image/jpeg")
    return f"data:{mime};base64,{b64}"


def run_mage_task(task_id, data_uris, prompt, mode, model_key, aspect_ratio,
                  end_data_uri=None, video_duration="5", video_audio=True,
                  video_format="16:9", nano_banana_v2_aspect_ratio=None):
    try:
        if task_id not in tasks: return
        update_task_state(task_id, {'status': "Çalışıyor"})

        email = generate_random_email()
        log_task(task_id, f"🎯 Kullanılan Email: {email}")

        session_req = requests.Session()
        session_req.headers.update({"user-agent": MAGE_HEADERS_BASE["user-agent"]})

        log_task(task_id, "📨 ADIM 1: Magic link gönderiliyor...")
        url_a1 = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_API_KEY}"
        req_a1 = requests.post(url_a1, headers=FIREBASE_HEADERS, json={
            "requestType": "EMAIL_SIGNIN", "email": email, "clientType": "CLIENT_TYPE_WEB",
            "continueUrl": CONTINUE_URL, "canHandleCodeInApp": True
        })
        if req_a1.status_code != 200: raise Exception(f"Adım 1 Hatası: {req_a1.text}")
        if task_id not in tasks: return

        log_task(task_id, "📬 ADIM 2: Gmail bağlantısı kuruluyor...")
        creds = None
        if os.path.exists("token.json"): creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)
            with open("token.json", "w") as token: token.write(creds.to_json())
        service = build("gmail", "v1", credentials=creds)

        log_task(task_id, "⏳ Magic link bekleniyor (Maks 120s)...")
        magic_url = None

        def govdeden_link_cek(mesaj):
            def parcalari_tara(payload):
                body_data = payload.get("body", {}).get("data", "")
                if body_data:
                    metin = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                    eslesen = re.findall(r'https://www\.mage\.space/[^\s"\'<>]+oobCode=[^\s"\'<>&]+(?:&[^\s"\'<>]*)*', metin)
                    if eslesen: return eslesen[0]
                for part in payload.get("parts", []):
                    sonuc = parcalari_tara(part)
                    if sonuc: return sonuc
                return None
            return parcalari_tara(mesaj["payload"])

        for deneme in range(24):
            if task_id not in tasks: return
            time.sleep(5)
            sonuc = service.users().messages().list(userId="me", q='subject:"Sign in to" newer_than:1d', maxResults=10).execute()
            for msg in sonuc.get("messages", []):
                detay = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
                headers = {h["name"]: h["value"] for h in detay["payload"]["headers"]}
                is_target_email = email.lower() in headers.get("To", "").lower()
                is_mage_mail = "Sign in to" in headers.get("Subject", "") and ("mage" in headers.get("Subject", "").lower() or "mage.space" in headers.get("From", "").lower())
                if is_mage_mail and is_target_email:
                    bulunan_link = govdeden_link_cek(detay)
                    if bulunan_link:
                        magic_url = bulunan_link.replace("&amp;", "&")
                        break
            if magic_url: break

        if not magic_url: raise Exception("❌ Mage magic link maili gelmedi.")
        if task_id not in tasks: return
        log_task(task_id, "🔗 Magic link bulundu!")

        params = parse_qs(urlparse(magic_url).query)
        oob_code = params.get("oobCode", [None])[0]
        if not oob_code: raise Exception("oobCode bulunamadı!")

        url_a5 = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithEmailLink?key={FIREBASE_API_KEY}"
        resp_a5 = requests.post(url_a5, headers=FIREBASE_HEADERS, json={"email": email, "oobCode": oob_code}).json()
        id_token, local_id = resp_a5["idToken"], resp_a5["localId"]

        log_task(task_id, "🍪 ADIM 6b: Session cookie alınıyor...")
        url_base = f"https://www.mage.space/explore?onboarding=1&apiKey={FIREBASE_API_KEY}&oobCode={oob_code}&mode=signIn&lang=en"
        h_6b = {**MAGE_HEADERS_BASE, "next-action": "4054c40ba429ef3e5e1d42ad95e138aff0a501ce3d", "next-router-state-tree": _router_state_tree(oob_code), "referer": url_base}
        resp_6b = session_req.post(url_base, headers=h_6b, data=json.dumps([id_token]))

        match = re.search(r'__session=([^;]+)', resp_6b.headers.get("set-cookie", ""))
        if match: session_req.cookies.set("__session", match.group(1), domain="www.mage.space", path="/")
        else: session_req.cookies.set("__session", id_token, domain="www.mage.space", path="/")

        log_task(task_id, "🌐 ADIM 7-10: Oturum açılış sinyalleri...")
        h_7 = {**MAGE_HEADERS_BASE, "next-action": "00fe3e87d33dbb585f5ffc95f3b64e31a040c71b7d", "next-router-state-tree": _router_state_tree(oob_code), "referer": url_base}
        session_req.post(url_base, headers=h_7, data="[]")
        h_8 = {**MAGE_HEADERS_BASE, "next-action": "7f458edefe7eaf76ee6672d8f31e6c44b748b529f1", "next-router-state-tree": _router_state_tree(oob_code), "referer": url_base}
        session_req.post(url_base, headers=h_8, data="[]")
        payload_9 = json.dumps([local_id, "$undefined"])
        h_9 = {**MAGE_HEADERS_BASE, "next-action": "60cca13a8cd0fe23bb362d3f090b3722353d636cd4", "next-router-state-tree": _router_state_tree(oob_code), "referer": url_base, "content-length": str(len(payload_9))}
        session_req.post(url_base, headers=h_9, data=payload_9)

        if task_id not in tasks: return

        log_task(task_id, "⚙️ ADIM 11: Settings değiştiriliyor...")
        h_11 = {**MAGE_HEADERS_BASE, "next-action": "40b85cfadb4ae6fba91cd71357a319d679a2e54a62", "next-router-state-tree": _settings_router_state_tree(), "referer": "https://www.mage.space/settings"}
        session_req.post("https://www.mage.space/settings", headers=h_11, data=json.dumps([{"rating": "M+", "moderation": ["suggestive", "nudity", "violence", "nsfw"]}]))

        h_12 = {**MAGE_HEADERS_BASE, "next-action": "60f4bfd3857dbe7fd3081411e4843c708e0bf3e3b8", "next-router-state-tree": _explore_router_state_tree(), "referer": "https://www.mage.space/explore"}

        cdn_urls = []
        for i, duri in enumerate(data_uris):
            log_task(task_id, f"📤 ADIM 12: Resim {i+1}/{len(data_uris)} yükleniyor...")
            payload_12 = json.dumps([duri, local_id])
            resp_12 = session_req.post("https://www.mage.space/explore", headers=h_12, data=payload_12.encode("utf-8"), timeout=120)
            cdn_url = _parse_cdn_url(resp_12.text)
            if cdn_url:
                cdn_urls.append(cdn_url)
                log_task(task_id, f"✅ CDN URL alındı ({i+1})")
            else:
                log_task(task_id, f"⚠️ Resim {i+1} yüklenemedi, atlandı.")

        if not cdn_urls:
            raise Exception("Hiçbir resim yüklenemedi.")

        main_cdn = cdn_urls[0]
        additional_cdns = cdn_urls[1:] if len(cdn_urls) > 1 else []
        log_task(task_id, f"✅ Toplam {len(cdn_urls)} resim yüklendi")

        cdn_url_end = None
        if mode == "video" and end_data_uri:
            log_task(task_id, "📤 ADIM 12b: End frame yükleniyor...")
            payload_12b = json.dumps([end_data_uri, local_id])
            resp_12b = session_req.post("https://www.mage.space/explore", headers=h_12, data=payload_12b.encode("utf-8"), timeout=120)
            cdn_url_end = _parse_cdn_url(resp_12b.text)
            if cdn_url_end:
                log_task(task_id, "✅ End frame CDN URL alındı.")
            else:
                log_task(task_id, "⚠️ End frame CDN URL alınamadı, end frame olmadan devam ediliyor.")

        if task_id not in tasks: return

        if mode == "video":
            video_cfg = VIDEO_MODELS.get(model_key, VIDEO_MODELS["peach_max"])
            log_task(task_id, f"🎬 ADIM 13: Video üretimi başlıyor ({video_cfg['model_id']})...")
            format_to_aspect = {"16:9": "cinema", "9:16": "portrait", "1:1": "square"}
            general_aspect = format_to_aspect.get(video_format, "cinema")
            arch_config = {
                "seed": None, "audio": video_audio, "prompt": prompt,
                "duration": str(video_duration), "model_id": video_cfg["model_id"],
                "fast_mode": True, "resolution": video_cfg["resolution"],
                "architecture": video_cfg["architecture"], "aspect_ratio": general_aspect,
                "image": main_cdn,
                "additional_images": [cdn_url_end] if cdn_url_end else None,
                "last_image": cdn_url_end if cdn_url_end else "$undefined",
            }
            if video_cfg["architecture"] == "peach_max":
                arch_config["peach_max_aspect_ratio"] = video_format
            elif video_cfg["architecture"] == "kiwi":
                arch_config["kiwi_aspect_ratio"] = video_format
            payload_13 = [{"architectureConfig": arch_config, "architectureConfigToSave": "$0:0:architectureConfig", "authToken": id_token, "conceptId": None, "activePowerPack": None}]
        else:
            model_config = MODELS.get(model_key, MODELS["mango-v2"])
            log_task(task_id, f"🎨 ADIM 13: Görsel üretimi başlıyor ({model_config['model_id']})...")
            arch_config = {
                "seed": None, "prompt": prompt, "model_id": model_config["model_id"],
                "fast_mode": True, "resolution": model_config["resolution"],
                "architecture": model_config["architecture"], "aspect_ratio": aspect_ratio,
                "prompt_extend": False, "additional_images": additional_cdns if additional_cdns else [],
                "image": main_cdn,
            }
            if model_config["architecture"] == "nano_banana_v2" and nano_banana_v2_aspect_ratio:
                arch_config["nano_banana_v2_aspect_ratio"] = nano_banana_v2_aspect_ratio
            payload_13 = [{"architectureConfig": arch_config, "architectureConfigToSave": "$0:0:architectureConfig", "authToken": id_token, "conceptId": None, "activePowerPack": None}]

        h_13 = {**MAGE_HEADERS_BASE, "next-action": "404f78770860c7f3f0d9688276b791a615bc4219a6", "next-router-state-tree": _explore_router_state_tree(), "referer": "https://www.mage.space/explore"}
        resp_13 = session_req.post("https://www.mage.space/explore", headers=h_13, data=json.dumps(payload_13).encode("utf-8"), timeout=120)

        h_match = re.search(r'"history_id":"([^"]+)"', resp_13.text)
        if not h_match: raise Exception("History ID alınamadı.")
        history_id = h_match.group(1)

        log_task(task_id, "⏳ ADIM 14: Sonuç bekleniyor...")
        time.sleep(15)

        url_14 = "https://www.mage.space/creations"
        payload_14 = json.dumps([local_id, 100, 0, {"status": "success", "type": "$undefined"}])
        h_14 = {**MAGE_HEADERS_BASE, "next-action": "7888a54672a9f4abf6abe443fc8d974f074759eaa0", "next-router-state-tree": _creations_router_state_tree(), "referer": "https://www.mage.space/creations"}

        result_url = None
        for _ in range(60):
            if task_id not in tasks: return
            resp_14 = session_req.post(url_14, headers=h_14, data=payload_14, timeout=30)
            if history_id in resp_14.text:
                for line in resp_14.text.splitlines():
                    if line.startswith("1:"):
                        try:
                            hist_data = json.loads(line[2:])
                            for h in hist_data.get("histories", []):
                                if h.get("id") == history_id:
                                    if h.get("status") == "success":
                                        data_block = h.get("result", {}).get("data", {})
                                        result_url = data_block.get("video") or data_block.get("image")
                                    elif h.get("status") == "failed":
                                        raise Exception("Üretim başarısız oldu!")
                        except Exception as ex:
                            if "başarısız" in str(ex): raise
                            pass

            if not result_url:
                vid_match = re.search(r'"video":"(https://cdn3\.mage\.space/[^"]+)"', resp_14.text)
                img_match = re.search(r'"image":"(https://cdn3\.mage\.space/temp/[^"]+)"', resp_14.text)
                if vid_match: result_url = vid_match.group(1)
                elif img_match: result_url = img_match.group(1)

            if result_url: break
            time.sleep(5)

        if result_url:
            label = "🎬 VİDEO" if mode == "video" else "✨ GÖRSEL"
            log_task(task_id, f"{label} HAZIR!")
            update_task_state(task_id, {'status': 'Tamamlandı', 'result_url': result_url, 'result_type': mode})
        else:
            raise Exception("Zaman aşımı - sonuç alınamadı.")

    except Exception as e:
        log_task(task_id, f"❌ HATA: {str(e)}")
        update_task_state(task_id, {'status': 'Hata'})


# ── LOGIN ROUTELARI ───────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if session.get('logged_in'):
        return redirect('/')
    error = False
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            return redirect('/')
        error = True
    return render_template('index.html', show_login=True, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── FLASK ROUTELARI ───────────────────────────────────────
@app.route('/')
@login_required
def index():
    return render_template('index.html', show_login=False, error=False)

@app.route('/start_task', methods=['POST'])
@login_required
def start_task():
    prompt       = request.form.get('prompt', 'hello')
    mode         = request.form.get('mode', 'image')
    model        = request.form.get('model', 'mango-v2')
    aspect_ratio = request.form.get('aspect_ratio', 'portrait')
    video_duration = request.form.get('video_duration', '5')
    video_audio    = request.form.get('video_audio', 'true').lower() == 'true'
    video_format   = request.form.get('video_format', '16:9')
    nano_banana_v2_aspect_ratio = request.form.get('nano_banana_v2_aspect_ratio', None)

    data_uris = []
    end_data_uri = None

    if mode == "video":
        if 'image' not in request.files:
            return jsonify({"error": "Start frame eksik"}), 400
        file = request.files['image']
        if file.filename == '':
            return jsonify({"error": "Dosya seçilmedi"}), 400
        data_uris.append(_file_to_data_uri(file))
        if 'end_image' in request.files:
            end_file = request.files['end_image']
            if end_file and end_file.filename != '':
                end_data_uri = _file_to_data_uri(end_file)
    else:
        files = request.files.getlist('images')
        if not files or all(f.filename == '' for f in files):
            if 'image' in request.files:
                file = request.files['image']
                if file.filename != '':
                    data_uris.append(_file_to_data_uri(file))
        else:
            for f in files[:10]:
                if f.filename != '':
                    data_uris.append(_file_to_data_uri(f))

    if not data_uris:
        return jsonify({"error": "En az 1 dosya yüklenmeli"}), 400

    task_id = str(uuid.uuid4())
    with task_lock:
        tasks[task_id] = {
            "status": "Başlıyor...",
            "logs": [],
            "result_url": None,
            "result_type": mode,
            "prompt": prompt,
            "model": model,
            "mode": mode,
        }

    thread = threading.Thread(
        target=run_mage_task,
        args=(task_id, data_uris, prompt, mode, model, aspect_ratio,
              end_data_uri, video_duration, video_audio, video_format, nano_banana_v2_aspect_ratio)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})

@app.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    with task_lock:
        if task_id in tasks: return jsonify(tasks[task_id])
    return jsonify({"error": "Görev bulunamadı"}), 404

@app.route('/get_tasks', methods=['GET'])
@login_required
def get_tasks():
    with task_lock:
        return jsonify(tasks)

@app.route('/delete_task/<task_id>', methods=['DELETE'])
@login_required
def delete_task(task_id):
    with task_lock:
        if task_id in tasks:
            del tasks[task_id]
            return jsonify({"success": True})
    return jsonify({"error": "Görev bulunamadı"}), 404

@app.route('/proxy_image')
@login_required
def proxy_image():
    url = request.args.get('url', '')
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    try:
        resp = requests.get(url, timeout=60)
        content_type = resp.headers.get('content-type', 'application/octet-stream')
        return Response(resp.content, mimetype=content_type)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/proxy_video')
@login_required
def proxy_video():
    """Video proxy - range request destekli, frame extraction için."""
    url = request.args.get('url', '')
    dl  = request.args.get('dl', '0') == '1'
    if not url:
        return jsonify({"error": "URL gerekli"}), 400

    range_header = request.headers.get('Range', None)
    req_headers = {}
    if range_header:
        req_headers['Range'] = range_header

    try:
        resp = requests.get(url, headers=req_headers, stream=True, timeout=60)
        response_headers = {
            'Content-Type': resp.headers.get('content-type', 'video/mp4'),
            'Accept-Ranges': 'bytes',
        }
        if 'Content-Length' in resp.headers:
            response_headers['Content-Length'] = resp.headers['Content-Length']
        if 'Content-Range' in resp.headers:
            response_headers['Content-Range'] = resp.headers['Content-Range']
        if dl:
            response_headers['Content-Disposition'] = 'attachment; filename="video.mp4"'

        def generate():
            for chunk in resp.iter_content(chunk_size=65536):
                yield chunk

        return Response(generate(), status=resp.status_code, headers=response_headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/save_prompt', methods=['POST'])
@login_required
def save_prompt():
    data = request.get_json()
    if not data or not data.get('text', '').strip():
        return jsonify({"error": "Prompt metni boş olamaz"}), 400
    prompt_id = str(uuid.uuid4())
    entry = {"id": prompt_id, "text": data['text'].strip(), "timestamp": int(time.time() * 1000)}
    with prompts_lock:
        saved_prompts[prompt_id] = entry
    return jsonify(entry)

@app.route('/get_prompts', methods=['GET'])
@login_required
def get_prompts():
    with prompts_lock:
        result = sorted(saved_prompts.values(), key=lambda p: p['timestamp'], reverse=True)
    return jsonify(result)

@app.route('/delete_prompt/<prompt_id>', methods=['DELETE'])
@login_required
def delete_prompt(prompt_id):
    with prompts_lock:
        if prompt_id in saved_prompts:
            del saved_prompts[prompt_id]
            return jsonify({"success": True})
    return jsonify({"error": "Prompt bulunamadı"}), 404

@app.route('/gallery_add', methods=['POST'])
@login_required
def gallery_add():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Veri eksik"}), 400
    with gallery_lock:
        gallery_items[:] = [i for i in gallery_items if i.get('id') != data.get('id')]
        gallery_items.insert(0, data)
        if len(gallery_items) > 200:
            gallery_items[:] = gallery_items[:200]
    return jsonify({"success": True})

@app.route('/get_gallery', methods=['GET'])
@login_required
def get_gallery():
    with gallery_lock:
        return jsonify(list(gallery_items))

@app.route('/delete_gallery/<item_id>', methods=['DELETE'])
@login_required
def delete_gallery(item_id):
    with gallery_lock:
        before = len(gallery_items)
        gallery_items[:] = [i for i in gallery_items if i.get('id') != item_id]
        if len(gallery_items) < before:
            return jsonify({"success": True})
    return jsonify({"error": "Öğe bulunamadı"}), 404

@app.route('/clear_gallery', methods=['DELETE'])
@login_required
def clear_gallery():
    with gallery_lock:
        gallery_items.clear()
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
