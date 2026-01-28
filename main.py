from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from db import get_db
from telegram_bot import get_application
from retrieve_canvas_data import check_and_send_reminders

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the bot application
    bot_app = get_application()
    
    # Store for access in endpoints
    app.state.bot_app = bot_app
    
    # Initialize and start the bot
    await bot_app.initialize()
    await bot_app.start()
    
    # Start polling in a non-blocking way
    # drop_pending_updates=True is often good for development to avoid processing old messages, 
    # but allowed_updates=Update.ALL_TYPES is safer default.
    await bot_app.updater.start_polling(drop_pending_updates=True)
    
    yield
    
    # Shutdown logic
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return FileResponse("./pages/index.html")


@app.get("/check_connection")
def check_connection():
    db = get_db()
    collections = db.collections()
    next(collections, None) 
    return {"status": "ok", "message": "Connected to Firestore"}

@app.api_route("/check_reminder", methods=["GET", "POST"])
async def check_reminders_handler(request: Request):
    bot_app = getattr(request.app.state, "bot_app", None)
    if not bot_app:
        return {"status": "error", "message": "Bot not initialized"}
        
    # Trigger reminder check logic
    # This runs sync (retrieve_all) + checks + sends.
    await check_and_send_reminders(bot_app.bot)
    
    return {"status": "ok", "message": "Reminders checked"}

