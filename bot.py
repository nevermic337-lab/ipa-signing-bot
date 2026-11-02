import os
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path

import requests
from flask import Flask, request, abort

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
MAX_INPUT = 20 * 1024 * 1024  # Telegram cloud Bot API download limit
MAX_OUTPUT = 50 * 1024 * 1024
Zsign = os.getenv("ZSIGN_PATH", "/usr/local/bin/zsign")

app = Flask(__name__)
sessions = {}
lock = threading.Lock()


def tg(method, data=None, files=None, timeout=120):
    response = requests.post(
        f"{BASE_URL}/{method}", data=data, files=files, timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload["result"]


def say(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text})


def remove_session(chat_id):
    with lock:
        session = sessions.pop(str(chat_id), None)
    if session:
        shutil.rmtree(session["dir"], ignore_errors=True)


def new_session(chat_id):
    folder = tempfile.mkdtemp(prefix="ipa-sign-")
    with lock:
        old = sessions.pop(str(chat_id), None)
        sessions[str(chat_id)] = {"dir": folder, "step": "p12"}
    if old:
        shutil.rmtree(old["dir"], ignore_errors=True)


def get_session(chat_id):
    with lock:
        return sessions.get(str(chat_id))


def download_document(document, destination):
    file_id = document["file_id"]
    size = int(document.get("file_size", 0))
    if size and size > MAX_INPUT:
        raise ValueError("File 20 MB se badi hai. Telegram Bot API download limit ki wajah se ise process nahi kar sakta.")
    info = tg("getFile", {"file_id": file_id})
    file_path = info["file_path"]
    url = f"{FILE_URL}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = 0
        with open(destination, "wb") as out:
            for chunk in r.iter_content(1024 * 256):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_INPUT:
                    raise ValueError("File 20 MB limit se badi hai.")
                out.write(chunk)


def process_update(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    chat_key = str(chat_id)
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        remove_session(chat_id)
        say(chat_id,
            "🍎 IPA Signing Bot\\n\\n"
            "Apni IPA sign karne ke liye /sign bhejein.\\n"
            "Aapka .p12 certificate aur mobileprovision profile sirf is job ke liye temporary rakhe jayenge.")
        return

    if text.startswith("/cancel"):
        remove_session(chat_id)
        say(chat_id, "Job cancel kar di. Temporary files delete kar diye gaye.")
        return

    if text.startswith("/sign"):
        new_session(chat_id)
        say(chat_id, "Step 1/4: Apni .p12 certificate file upload karein.")
        return

    session = get_session(chat_id)
    if not session:
        say(chat_id, "Pehle /sign bhejein. Cancel karne ke liye /cancel.")
        return

    if session["step"] == "password":
        if not text or text.startswith("/"):
            say(chat_id, "P12 password text ke roop mein bhejein, ya /cancel.")
            return
        # Best-effort: remove the password message from the chat.
        try:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": message["message_id"]})
        except Exception:
            pass
        session["password"] = text
        session["step"] = "signing"
        say(chat_id, "⏳ Signing shuru ho rahi hai. Thoda intezar karein...")
        try:
            output = os.path.join(session["dir"], "signed.ipa")
            command = [
                Zsign, "-f", "-k", session["p12"], "-p", session["password"],
                "-m", session["profile"], "-o", output, session["ipa"]
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=600, check=False
            )
            if result.returncode != 0 or not os.path.isfile(output):
                details = (result.stderr or result.stdout or "Unknown signing error")[-2500:]
                say(chat_id, "❌ Signing fail hui. Certificate/profile, password, bundle ID aur device registration check karein.\\n\\n" + details)
                remove_session(chat_id)
                return
            if os.path.getsize(output) > MAX_OUTPUT:
                say(chat_id, "Signed IPA 50 MB se badi hai; Telegram Bot API se bhejna possible nahi.")
                remove_session(chat_id)
                return
            with open(output, "rb") as ipa_file:
                tg("sendDocument",
                   data={"chat_id": chat_id, "caption": "✅ Signed IPA ready"},
                   files={"document": ("signed.ipa", ipa_file, "application/octet-stream")},
                   timeout=180)
            remove_session(chat_id)
        except subprocess.TimeoutExpired:
            say(chat_id, "❌ Signing timeout ho gaya. Chhoti IPA try karein.")
            remove_session(chat_id)
        except Exception as exc:
            say(chat_id, f"❌ Error: {str(exc)[:1000]}")
            remove_session(chat_id)
        return

    document = message.get("document")
    if not document:
        say(chat_id, "Is step par file upload karein, ya /cancel bhejein.")
        return

    filename = (document.get("file_name") or "").lower()
    try:
        if session["step"] == "p12":
            if not filename.endswith((".p12", ".pfx")):
                say(chat_id, "Please .p12 ya .pfx file upload karein.")
                return
            path = os.path.join(session["dir"], "certificate.p12")
            download_document(document, path)
            session["p12"] = path
            session["step"] = "profile"
            say(chat_id, "Step 2/4: Ab matching .mobileprovision profile upload karein.")
        elif session["step"] == "profile":
            if not filename.endswith(".mobileprovision"):
                say(chat_id, "Please .mobileprovision file upload karein.")
                return
            path = os.path.join(session["dir"], "profile.mobileprovision")
            download_document(document, path)
            session["profile"] = path
            session["step"] = "ipa"
            say(chat_id, "Step 3/4: Ab .ipa file upload karein (max 20 MB).")
        elif session["step"] == "ipa":
            if not filename.endswith(".ipa"):
                say(chat_id, "Please .ipa file upload karein.")
                return
            path = os.path.join(session["dir"], "input.ipa")
            download_document(document, path)
            session["ipa"] = path
            session["step"] = "password"
            say(chat_id, "Step 4/4: Ab apne .p12 certificate ka password bhejein.\\n"
                         "Security: password message delete karne ki koshish hogi. /cancel se job rokein.")
    except Exception as exc:
        say(chat_id, f"❌ File receive nahi hui: {str(exc)[:1000]}")
        remove_session(chat_id)


@app.get("/")
def health():
    return "IPA Signing Bot is running", 200


@app.post("/webhook/<secret>")
def webhook(secret):
    if secret != WEBHOOK_SECRET:
        abort(403)
    update = request.get_json(silent=True) or {}
    # Process outside the webhook request so Telegram gets a quick 200 response.
    threading.Thread(target=process_update, args=(update,), daemon=True).start()
    return "ok", 200


def register_webhook():
    public_url = os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:
        url = f"{public_url}/webhook/{WEBHOOK_SECRET}"
        try:
            tg("setWebhook", {"url": url, "drop_pending_updates": True})
        except Exception as exc:
            print("Webhook registration failed:", exc)


register_webhook()
