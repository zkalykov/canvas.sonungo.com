from canvasapi.exceptions import InvalidAccessToken
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
from db import get_db
from canvas_initialize import decrypt_token, get_upcoming_assignments

from datetime import datetime

def calculate_notification_level(deadline_str):
    try:
        dt_utc = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
        now = datetime.now().astimezone()
        remaining = dt_utc - now
        minutes_left = remaining.total_seconds() / 60
    except Exception:
        return 0, 0 # level, minutes_left
        
    if minutes_left <= 0: return 9, minutes_left
    elif minutes_left <= 5: return 8, minutes_left
    elif minutes_left <= 10: return 7, minutes_left
    elif minutes_left <= 15: return 6, minutes_left
    elif minutes_left <= 30: return 5, minutes_left
    elif minutes_left <= 60: return 4, minutes_left
    elif minutes_left <= 180: return 3, minutes_left
    elif minutes_left <= 360: return 2, minutes_left
    elif minutes_left <= 720: return 1, minutes_left
    return 0, minutes_left


async def sync_user_data(user_id: str, db=None, bot=None) -> int:
    """
    Syncs homeworks for a single user.
    Returns the number of assignments synced.
    """
    if db is None:
        db = get_db()
        
    user_ref = db.collection("users").document(user_id)
    doc = user_ref.get()
    
    if not doc.exists:
        return 0
        
    data = doc.to_dict()
    
    # Check if token is already marked invalid
    if data.get("canvas_token_status") == "invalid":
        return 0
        
    encrypted_token = data.get("canvas_token")
    canvas_url = data.get("canvas_url")
    
    if not encrypted_token or not canvas_url:
        return 0
        
    try:
        # Decrypt token
        token = decrypt_token(encrypted_token)
        
        # Fetch assignments
        assignments = get_upcoming_assignments(token, canvas_url)
        
        # Save to Firestore
        homeworks_ref = db.collection("homeworks")
        
        count = 0
        batch = db.batch() 
        # Using individual set operations for simplicity. 
        # (Batch limit is 500, unlikely to be exceeded per user)
        
        for hw in assignments:
            hw_doc = {
                "telegram_id": user_id,
                "assignment_id": hw['assignment_id'],
                "course_code": hw['course_code'],
                "course_name": hw['course_name'],
                "homework_name": hw['homework_name'],
                "deadline": hw['deadline']
            }
            # Composite key: user_id + assignment_id
            doc_id = f"{user_id}_{hw['assignment_id']}"
            doc_ref = homeworks_ref.document(doc_id)
            
            # Check existing doc to preserve local notification setting
            doc_snap = doc_ref.get()
            
            # Defaults
            current_notification = "on"
            
            # Smart Level Init:
            # If new doc, calculate level based on time remaining to avoid immediate spam
            calc_level, _ = calculate_notification_level(hw['deadline'])
            current_level = calc_level
            
            if doc_snap.exists:
                data = doc_snap.to_dict()
                val = data.get("notification", "on")
                if val == 1: val = "on"
                elif val == 0: val = "off"
                current_notification = val
                
                # If existing, keep existing level (unless we want to force update?)
                # Usually we trust the stored level.
                current_level = data.get("notification_level", 0)
            
            # Status comes strictly from Canvas
            # We trust Canvas. If user submits, Canvas says True.
            hw_doc["is_completed"] = hw.get('is_submitted', False)
            
            # Notification is strictly user preference (preserved)
            hw_doc["notification"] = current_notification
            hw_doc["notification_level"] = current_level
            
            # Set the document
            doc_ref.set(hw_doc)
            count += 1
            
        return count


    except InvalidAccessToken:
        print(f"Invalid Token for user {user_id}")
        # Mark as invalid to prevent future checks
        user_ref.update({"canvas_token_status": "invalid"})
        
        # Notify user if bot instance is available
        if bot:
            try:
                keyboard = [[InlineKeyboardButton("Update my token", callback_data="UPDATE_TOKEN")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "<b>Canvas Token Invalid</b>\n\n"
                        "Your Canvas token has expired or is no longer valid. "
                        "We have paused syncing your assignments.\n\n"
                        "Please update your token to resume."
                    ),
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            except Exception as e:
                print(f"Failed to send invalid token msg: {e}")
        return 0

    except Exception as e:
        print(f"Sync Error for {user_id}: {e}")
        # Fail silently for security (no logging of token errors here either)
        return 0

async def retrieve_all_canvas_data(bot=None) -> bool:
    """
    Iterates through all users and updates their homework data.
    Returns True if all jobs finish (even if some fail individually, the job 'finished').
    """
    try:
        db = get_db()
        users_ref = db.collection("users")
        # Stream all users
        users = users_ref.stream()
        
        # Create tasks for all users
        tasks = []
        for user_doc in users:
            user_id = user_doc.id
            tasks.append(sync_user_data(user_id, db, bot))
            
        # Run all syncs concurrently
        if tasks:
            await asyncio.gather(*tasks)
            
        return True
    except Exception:
        return False

async def check_and_send_reminders(bot):
    """
    1. Syncs all users.
    2. Checks database for due reminders.
    3. Sends notifications if thresholds are met.
    """
    # 1. Sync
    await retrieve_all_canvas_data(bot)
    
    db = get_db()
    # 2. Iterate DB
    docs = db.collection("homeworks").stream()
    
    # Local import to avoid circular dependency
    from telegram_bot import send_reminder_to_user
    
    for doc in docs:
        hw = doc.to_dict()
        user_id = hw.get("telegram_id")
        
        # Skip if completed or notification off
        if hw.get("is_completed", False): continue
        
        # Parse Deadline for Deletion Check
        try:
             deadline_str = hw.get('deadline')
             if deadline_str:
                  dt_utc = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
                  now = datetime.now().astimezone()
                  
                  # If overdue by more than 1 hour (3600 seconds), DELETE it
                  if (now - dt_utc).total_seconds() > 3600:
                      doc.reference.delete()
                      continue
        except Exception:
             pass

        val = hw.get("notification", "on")
        if val == 0 or val == "off": continue
        
        # Calculate level
        target_level, minutes_left = calculate_notification_level(hw['deadline'])
        current_level = hw.get("notification_level", 0)
        
        custom_header = ""
        
        # Determine Custom Header based on target_level
        # (Only needed if we are sending)
        if target_level > current_level:
            if target_level == 9: custom_header = "<b>Missed Assignment!</b>"
            elif target_level == 8: custom_header = "<b>5 minutes left!</b>"
            elif target_level == 7: custom_header = "<b>10 minutes left!</b>"
            elif target_level == 6: custom_header = "<b>15 minutes left!</b>"
            elif target_level == 5: custom_header = "<b>30 minutes left!</b>"
            elif target_level == 4: custom_header = "<b>1 hour left!</b>"
            elif target_level == 3: custom_header = "<b>3 hours left!</b>"
            elif target_level == 2: custom_header = "<b>6 hours left!</b>"
            elif target_level == 1: custom_header = "<b>12 hours left!</b>"
                 
            success = await send_reminder_to_user(bot, user_id, hw, custom_header)
            if success:
                doc.reference.update({"notification_level": target_level})
