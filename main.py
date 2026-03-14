import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from telegram import Update
from db import get_db
from telegram_bot import get_application
from retrieve_canvas_data import check_and_send_reminders
from fastapi.middleware.cors import CORSMiddleware
from fastapi import HTTPException
from datetime import datetime, timezone
from canvas_initialize import decrypt_token

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the bot application
    bot_app = get_application()
    
    # Store for access in endpoints
    app.state.bot_app = bot_app
    
    # Initialize and start the bot
    await bot_app.initialize()
    await bot_app.start()
    
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
    
    return {"status": "ok", "message": "Reminders checked"}

@app.post("/api/portal/auth")
async def verify_portal_auth(request: Request):
    data = await request.json()
    authcode = data.get("code")
    
    if not authcode:
        raise HTTPException(status_code=400, detail="Missing auth code")
        
    db = get_db()
    auth_ref = db.collection("auth_codes").document(authcode)
    auth_doc = auth_ref.get()
    
    if not auth_doc.exists:
        raise HTTPException(status_code=404, detail="Invalid auth code")
        
    auth_data = auth_doc.to_dict()
    
    if auth_data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Auth code already used")
        
    expires_at = auth_data.get("expires_at")
    if expires_at and expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Auth code expired")
        
    # Mark as used
    auth_ref.update({"status": "used"})
    
    user_id = auth_data.get("user")
    user_ref = db.collection("users").document(user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
        
    user_data = user_doc.to_dict()
    encrypted_token = user_data.get("canvas_token")
    canvas_url = user_data.get("canvas_url")
    
    if not encrypted_token or not canvas_url:
        raise HTTPException(status_code=400, detail="Incomplete user data")
        
    try:
        decrypted_token = decrypt_token(encrypted_token)
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to decrypt token")
        
    return {
        "status": "success",
        "canvas_url": canvas_url,
        "canvas_token": decrypted_token
    }
