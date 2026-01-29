import os
import html
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.warnings import PTBUserWarning
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv
from db import get_db
from canvas_initialize import verify_canvas_token, encrypt_token, search_canvas_institution
from retrieve_canvas_data import sync_user_data
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

# Filter out the specific PTB warning about CallbackQueryHandler
warnings.filterwarnings("ignore", category=PTBUserWarning)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_API")

# States for ConversationHandler
WAITING_FOR_SEARCH_QUERY = 1
WAITING_FOR_TOKEN = 2

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    db = get_db()
    
    # Check if user exists
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        if "canvas_url" in data and "canvas_token" in data:
            await update.message.reply_text("Welcome back! You are already connected.")
            return ConversationHandler.END
    
    await update.message.reply_text(
        "Welcome! It looks like you are not connected yet.\n"
        "To get started, please tell me the <b>name of your university</b> (e.g., 'North American University', 'Wisconsin' or 'Harvard').",
        parse_mode='HTML'
    )
    return WAITING_FOR_SEARCH_QUERY

async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    
    if len(query) < 3:
        await update.message.reply_text("Please enter a longer name to search.")
        return WAITING_FOR_SEARCH_QUERY
        
    await update.message.reply_text("Searching for your university...")
    
    results = search_canvas_institution(query)
    
    if not results:
        await update.message.reply_text("I couldn't find any universities matching that name. Please try again.")
        return WAITING_FOR_SEARCH_QUERY
        
    # Limit to top 5
    top_results = results[:5]
    
    keyboard = []
    for account in top_results:
        # Callback data: domain
        label = f"{account['name']} ({account['domain']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=account['domain'])])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please select your university:", reply_markup=reply_markup)
    
    
    return WAITING_FOR_SEARCH_QUERY

async def handle_university_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    domain = query.data
    # We might want to get the name too, but we only passed domain.
    # We can just say "Selected".
    
    context.user_data['canvas_url'] = domain
    
    await query.edit_message_text(text=f"Selected University Domain: {domain}")
    
    # Construct profile settings URL
    profile_url = domain
    if not profile_url.startswith("http"):
        profile_url = f"https://{profile_url}"
    profile_url = f"{profile_url.rstrip('/')}/profile/settings"
    
    # Add buttons: Search Again, and Find Token (link to settings)
    keyboard = [
        [InlineKeyboardButton("Search Again / Change University", callback_data="SEARCH_AGAIN")],
        [InlineKeyboardButton("Find Token", url=profile_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="Great! Now, please send me your <b>Canvas Token</b>.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    
    return WAITING_FOR_TOKEN

async def handle_max_speed_delete(message):
    try:
        await message.delete()
    except Exception:
        pass

async def handle_search_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Okay, let's search again. Please tell me the <b>name of your university</b>.", parse_mode='HTML')
    return WAITING_FOR_SEARCH_QUERY

async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_text = update.message.text.strip()
    
    # Check for commands or explicit cancel/search keywords manually if user types them instead of clicking
    # (Though buttons are primary, this handles "Search" text)
    if token_text.lower() in ["search", "search again", "cancel", "/start"]:
        if token_text.lower() == "/start":
             pass
        else:
             await update.message.reply_text("Okay, let's search again. Please tell me the *name of your university*.")
             return WAITING_FOR_SEARCH_QUERY

    # Trigger message deletion
    asyncio.create_task(handle_max_speed_delete(update.message))

    user_id = str(update.effective_user.id)
    canvas_url = context.user_data.get('canvas_url')
    
    # Verify token
    await update.message.reply_text("Verifying...")
    result = verify_canvas_token(token_text, canvas_url)
    
    if result:
        university_name, user_canvas_name = result
        
        # Encrypt token
        encrypted_token = encrypt_token(token_text)
        
        # Save to Firestore (Minimal Data)
        db = get_db()
        user_data = {
            "telegram_id": user_id,
            "canvas_token": encrypted_token,
            "canvas_url": canvas_url,
            "notification": "on"
        }
        db.collection("users").document(user_id).set(user_data)
        
        await update.message.reply_text(f"Connected: {university_name} - {user_canvas_name}")
        
        # Trigger initial sync
        await update.message.reply_text("Syncing your homeworks for the next 24 hours...")
        count = await sync_user_data(user_id)
        if count > 0:
             await update.message.reply_text(f"Found {count} assignments due soon! check /assignments")
        else:
             await update.message.reply_text("No 24h deadline assignments found right now.")
             
        return ConversationHandler.END
    else:
        await update.message.reply_text("Invalid Canvas Token. Please check your token and try again.")
        return WAITING_FOR_TOKEN

def format_assignment_message(hw):
    # Determine status & notification
    is_completed = hw.get("is_completed", False)
    val = hw.get("notification", "on")
    if val == 1: val = "on"
    elif val == 0: val = "off"
    is_notify_on = (val == "on")
    
    # Format Deadline
    raw_deadline = hw['deadline']
    try:
        # Parse UTC string (Canvas usually sends 'Z' at the end)
        dt_utc = datetime.fromisoformat(raw_deadline.replace('Z', '+00:00'))
        # Convert to local system time
        dt_local = dt_utc.astimezone()
        # Format nicely: "Wednesday, Jan 28 at 11:59 PM"
        formatted_deadline = dt_local.strftime("%A, %b %d at %I:%M %p")
        
        # Calculate time remaining
        now = datetime.now().astimezone()
        remaining = dt_local - now
        
        if is_completed:
            time_left_str = "<b>Completed</b>"
        elif remaining.total_seconds() > 0:
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            time_left_str = f"Due in {hours}h {minutes}m"
        else:
            time_left_str = "Overdue"
    except Exception:
        formatted_deadline = raw_deadline
        time_left_str = ""
        
    msg_text = (
        f"{time_left_str}\n"
        f"Name: <b>{html.escape(hw['homework_name'])}</b>\n"
        f"Course: {html.escape(hw['course_name'])} ({html.escape(hw['course_code'])})\n"
        f"Deadline: {formatted_deadline}"
    )
    
    btn_text = "Turn off reminder" if is_notify_on else "Turn on reminder"
    # Callback data format: prefix + assignment_id
    assign_id = hw.get('assignment_id', 'unknown')
    
    keyboard = [[InlineKeyboardButton(btn_text, callback_data=f"TOGGLE_NOTIF_{assign_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    return msg_text, reply_markup

async def send_reminder_to_user(bot, user_id, hw, custom_header=""):
    text, markup = format_assignment_message(hw)
    if custom_header:
        text = f"{custom_header}\n{text}"
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=markup)
        return True
    except Exception:
        return False

async def assignments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # await update.message.reply_text("Resyncing your assignments...")
    # await sync_user_data(user_id)
    # Syncing handled by scheduler
    pass
    
    # Fetch from DB to display
    db = get_db()
    docs = db.collection("homeworks").where(field_path="telegram_id", op_string="==", value=user_id).stream()
    
    found_any = False
    for doc in docs:
        found_any = True
        hw = doc.to_dict()
        
        msg_text, reply_markup = format_assignment_message(hw)
        await update.message.reply_text(msg_text, parse_mode='HTML', reply_markup=reply_markup)
    
    if not found_any:
        await update.message.reply_text("No upcoming assignments found in the next 24 hours.")

async def handle_notification_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Acknowledge interaction
    
    user_id = str(update.effective_user.id)
    data = query.data
    
    # Extract assignment_id
    if not data.startswith("TOGGLE_NOTIF_"):
        return
        
    assignment_id = data.replace("TOGGLE_NOTIF_", "")
    doc_id = f"{user_id}_{assignment_id}"
    
    db = get_db()
    doc_ref = db.collection("homeworks").document(doc_id)
    doc = doc_ref.get()
    
    if doc.exists:
        hw = doc.to_dict()
        
        val = hw.get("notification", "on")
        if val == 1: val = "on"
        elif val == 0: val = "off"
        current_status = val
        
        new_status = "off" if current_status == "on" else "on"
        
        # Update DB - notification ONLY
        doc_ref.update({"notification": new_status})
        
        # Update Button
        is_notify_on = (new_status == "on")
        btn_text = "Turn off reminder" if is_notify_on else "Turn on reminder"
        keyboard = [[InlineKeyboardButton(btn_text, callback_data=data)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        
        status_text = "enabled" if is_notify_on else "disabled"
        await query.answer(f"Notification {status_text}!")
    else:
        await query.answer("Assignment not found.", show_alert=True)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Delete my Data", callback_data="SETTINGS_DEL_CONFIRM")],
        [InlineKeyboardButton("About", callback_data="SETTINGS_ABOUT")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("<b>Settings</b>\n\nChoose an option:", reply_markup=reply_markup, parse_mode='HTML')

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = str(update.effective_user.id)
    
    if data == "SETTINGS_ABOUT":
        back_btn = [[InlineKeyboardButton("Back", callback_data="SETTINGS_BACK")]]
        about_text = (
            "<b>Canvas Bot</b>\n\n"
            "This bot helps you stay on top of your Canvas assignments.\n\n"
            "<b>Data We Save:</b>\n"
            "• Telegram ID (to identify you)\n"
            "• Canvas URL (to convert deadlines)\n"
            "• Homework Data (Name, Course, Deadline)\n\n"
            "<b>Security:</b>\n"
            "• Your <b>Canvas Token</b> is encrypted via <b>Google Cloud KMS</b>.\n"
            "• It is decrypted <i>only</i> when syncing assignments and immediately discarded.\n"
            "• Even the developer cannot decrypt or see your token."
        )
        await query.edit_message_text(about_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(back_btn))
        
    elif data == "SETTINGS_BACK":
        # Restore main settings menu
        keyboard = [
            [InlineKeyboardButton("Delete my Data", callback_data="SETTINGS_DEL_CONFIRM")],
            [InlineKeyboardButton("About", callback_data="SETTINGS_ABOUT")]
        ]
        await query.edit_message_text("**Settings**\n\nChoose an option:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    elif data == "SETTINGS_DEL_CONFIRM":
        # Ask for confirmation
        keyboard = [
            [InlineKeyboardButton("Yes, Delete Everything", callback_data="SETTINGS_DEL_DO")],
            [InlineKeyboardButton("No, Cancel", callback_data="SETTINGS_BACK")]
        ]
        warn_text = (
            "<b>Are you sure?</b>\n\n"
            "This will delete your:\n"
            "- Canvas Token connection\n"
            "- Saved University URL\n"
            "- All synced homework data\n\n"
            "This action cannot be undone."
        )
        await query.edit_message_text(warn_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "SETTINGS_DEL_DO":
        db = get_db()
        
        # Delete User
        db.collection("users").document(user_id).delete()
        
        # Delete Homeworks
        # Note: Ideally usage of batch commits for atomic operations
        batch = db.batch()
        docs = db.collection("homeworks").where(field_path="telegram_id", op_string="==", value=user_id).stream()
        count = 0
        deleted_count = 0
        
        for doc in docs:
            batch.delete(doc.reference)
            count += 1
            deleted_count += 1
            if count >= 400: # Firestore batch limit 500
                batch.commit()
                batch = db.batch()
                count = 0
        
        if count > 0:
            batch.commit()
            
        await query.edit_message_text(f"Account and {deleted_count} assignments deleted.\n\nType /start to restart.")

import re

async def monitor_token_leak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Monitors ordinary messages for potential accidental token leaks.
    If a message looks like a Canvas token (digit~longstring), delete it and warn.
    """
    message_text = update.message.text
    if not message_text:
        return

    # Canvas tokens standard format: integer~alphanum (min 20 chars for safety)
    # Regex checks for the pattern.
    token_pattern = r"^\s*\d+~[A-Za-z0-9\-_]{20,}\s*$"
    
    if re.match(token_pattern, message_text):
        try:
            await update.message.delete()
            await update.message.reply_text(
                "<b>Security Alert</b>\n\n"
                "It looks like you sent a Canvas Token.\n"
                "I deleted the message for your safety.\n"
                "Please do not send your token in the chat unless specifically asked.",
                parse_mode='HTML'
            )
        except Exception as e:
            # Could fail if bot lacks delete permissions, but usually works
            await update.message.reply_text("⚠️ Please delete your previous message. It looks like a token.")

async def delete_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes any image/media upload."""
    try:
        await update.message.delete()
    except Exception:
        pass

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

def get_application():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_API not found in environment variables.")
        
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("assignments", assignments_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(handle_notification_toggle, pattern="^TOGGLE_NOTIF_"))
    application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern="^SETTINGS_"))
    
    token_pattern = r"^\s*\d+~[A-Za-z0-9\-_]{20,}\s*$"
    image_filter = filters.PHOTO
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_SEARCH_QUERY: [
                MessageHandler(image_filter, delete_image),
                MessageHandler(filters.Regex(token_pattern), monitor_token_leak),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_query),
                CallbackQueryHandler(handle_university_selection)
            ],
            WAITING_FOR_TOKEN: [
                MessageHandler(image_filter, delete_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token),
                CallbackQueryHandler(handle_search_again, pattern="^SEARCH_AGAIN$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(conv_handler)
    
    # Global handler for potential leaks when not in a conversation state
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor_token_leak))
    application.add_handler(MessageHandler(image_filter, delete_image))
    
    return application

if __name__ == '__main__':
    try:
        application = get_application()
        print("Bot is running...")
        application.run_polling()
    except Exception as e:
        print(e)
        exit(1)
