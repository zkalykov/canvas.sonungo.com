import os
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from telegram import BotCommand, Update
from db import get_db
from telegram_bot import get_application
from retrieve_canvas_data import check_and_send_reminders
from fastapi.middleware.cors import CORSMiddleware
from fastapi import HTTPException
from datetime import datetime, timezone
from login_approval import create_login_request, check_login_request
from web_sessions import check_session, cleanup_login_records, end_session

# Commands shown in Telegram's "/" menu.
BOT_COMMANDS = [
    BotCommand("start", "Connect your Canvas account"),
    BotCommand("assignments", "Your upcoming assignments"),
    BotCommand("portal", "Log in to the Canvas Dashboard website"),
    BotCommand("sessions", "See and log out your website sessions"),
    BotCommand("status", "Your connection status"),
    BotCommand("settings", "Notifications and your data"),
    BotCommand("about", "What this bot stores and why"),
]


def require_portal_key(request: Request):
    """
    The /api/portal/* endpoints are for the Canvas Dashboard's server only. With
    PORTAL_API_KEY set, calls must send the same value in the X-Portal-Key header.
    """
    expected = os.getenv("PORTAL_API_KEY", "").strip()
    if not expected:
        return
    given = request.headers.get("x-portal-key", "")
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid portal key")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the bot application
    bot_app = get_application()
    
    # Store for access in endpoints
    app.state.bot_app = bot_app
    
    # Initialize and start the bot
    await bot_app.initialize()
    await bot_app.start()

    try:
        await bot_app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        print(f"Could not set the bot's command menu: {e}")
    if not os.getenv("PORTAL_API_KEY", "").strip():
        print("PORTAL_API_KEY is not set: the /api/portal endpoints accept calls from anyone.")
    
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        # Webhook Mode (Cloud Run)
        # Ensure URL has no trailing slash before appending path
        webhook_url = webhook_url.rstrip("/")
        await bot_app.bot.set_webhook(url=f"{webhook_url}/webhook")
        print(f"Webhook set to {webhook_url}/webhook")
    else:
        # Polling Mode (Local Dev)
        print("WEBHOOK_URL not found, starting polling...")
        await bot_app.updater.start_polling(drop_pending_updates=True)
    
    yield
    
    # Shutdown logic
    if not webhook_url:
        await bot_app.updater.stop()
        
    await bot_app.stop()
    await bot_app.shutdown()

from fastapi.staticfiles import StaticFiles

app = FastAPI(lifespan=lifespan)

portal_domain = os.getenv("PORTAL_DOMAIN", "http://localhost:3000").rstrip("/")
# Enforce HTTPS if not localhost
if not portal_domain.startswith("http://localhost"):
    if portal_domain.startswith("http://"):
        portal_domain = portal_domain.replace("http://", "https://", 1)
    elif not portal_domain.startswith("https://"):
        portal_domain = f"https://{portal_domain}"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[portal_domain],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc: Exception):
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/404.html"), status_code=404)

app.mount("/media", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "media")), name="media")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    bot_app = request.app.state.bot_app
    data = await request.json()
    try:
        update = Update.de_json(data, bot_app.bot)
        if update:
             await bot_app.process_update(update)
    except Exception as e:
        print(f"Webhook error: {e}")
    return Response(content="OK", status_code=200)

@app.get("/get_canvas_token")
async def read_get_token():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/get_canvas_token.html"))

@app.get("/")
async def read_root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/index.html"))

@app.get("/about")
async def read_about():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/about.html"))

@app.get("/github")
async def read_github():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/github.html"))

@app.get("/legal")
async def read_legal():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/legal.html"))


@app.get("/dashboard")
async def read_dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "pages/dashboard.html"))


@app.get("/check_connection")
def check_connection():
    try:
        db = get_db()
        # Verify connectivity by fetching list of collections (lazy, so iterate once)
        collections = db.collections()
        next(collections, None) 
        return {"status": "ok", "message": "Connected to Firestore"}
    except Exception as e:
        print(f"Firestore Connection Error: {e}")
        return {"status": "error", "message": str(e)}

@app.api_route("/check_reminder", methods=["GET", "POST"])
async def check_reminders_handler(request: Request):
    # Security Check
    expected_secret = os.getenv("SCHEDULER_SECRET")
    if expected_secret:
        auth_header = request.headers.get("X-Scheduler-Secret")
        if not auth_header or auth_header != expected_secret:
            return Response(content="Forbidden", status_code=403)

    bot_app = getattr(request.app.state, "bot_app", None)
    if not bot_app:
        return {"status": "error", "message": "Bot not initialized"}
        
    # Trigger reminder check logic
    # This runs sync (retrieve_all) + checks + sends.
    await check_and_send_reminders(bot_app.bot)

    # Housekeeping: old one-time codes, login requests and unused sessions.
    try:
        print(f"Cleaned up login records: {cleanup_login_records()}")
    except Exception as e:
        print(f"Login record cleanup failed: {e}")
    
    return {"status": "ok", "message": "Reminders checked"}

@app.post("/api/portal/login/request")
async def portal_login_request(request: Request):
    """Web dashboard opened a one-time link: ask the user on Telegram to approve."""
    require_portal_key(request)
    data = await request.json()
    code = str(data.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing auth code")
    return await create_login_request(
        request.app.state.bot_app.bot,
        code,
        str(data.get("device") or ""),
        str(data.get("location") or ""),
        str(data.get("ip") or ""),
    )


@app.post("/api/portal/login/status")
async def portal_login_status(request: Request):
    """Web dashboard polls here; returns Canvas credentials once the user approved."""
    require_portal_key(request)
    data = await request.json()
    return check_login_request(str(data.get("request_id") or ""), str(data.get("poll_token") or ""))


@app.post("/api/portal/session/check")
async def portal_session_check(request: Request):
    """Web dashboard asks whether a session is still active (not logged out in Telegram, not idle)."""
    require_portal_key(request)
    data = await request.json()
    return check_session(str(data.get("session_id") or ""), str(data.get("session_secret") or ""))


@app.post("/api/portal/session/end")
async def portal_session_end(request: Request):
    """Web dashboard logged out."""
    require_portal_key(request)
    data = await request.json()
    return end_session(str(data.get("session_id") or ""), str(data.get("session_secret") or ""))
