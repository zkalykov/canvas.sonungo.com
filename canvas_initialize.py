import os
from urllib.parse import urlparse
from canvasapi import Canvas
from canvasapi.exceptions import InvalidAccessToken
from google.cloud import kms
import base64
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
KEY_RING_ID = "canvas-reminder-ring"
CRYPTO_KEY_ID = "canvas-token-key"

def _get_kms_client_and_key():
    if not PROJECT_ID:
        raise ValueError("PROJECT_ID not found in environment variables")
    
    client = kms.KeyManagementServiceClient()
    key_name = client.crypto_key_path(PROJECT_ID, "global", KEY_RING_ID, CRYPTO_KEY_ID)
    return client, key_name

def encrypt_token(token: str) -> str:
    client, key_name = _get_kms_client_and_key()
    
    # Convert to bytes
    token_bytes = token.encode("utf-8")
    
    # Encrypt
    response = client.encrypt(request={'name': key_name, 'plaintext': token_bytes})
    
    # Return Base64 encoded ciphertext
    return base64.b64encode(response.ciphertext).decode("utf-8")

def decrypt_token(encrypted_token_str: str) -> str:
    client, key_name = _get_kms_client_and_key()
    
    # Decode Base64 to bytes
    ciphertext = base64.b64decode(encrypted_token_str)
    
    # Decrypt
    response = client.decrypt(request={'name': key_name, 'ciphertext': ciphertext})
    
    # Return plaintext
    return response.plaintext.decode("utf-8")

def verify_canvas_token(token: str, base_url: str):
    """
    Verifies the token by attempting to fetch the current user.
    Returns a tuple (university_name, user_name) if successful, else None.
    """
    try:
        # Ensure base_url has scheme
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
            
        canvas = Canvas(base_url, token)
        user = canvas.get_current_user()
        user_name = user.name
        
        # Derive university name from URL
        domain = urlparse(base_url).netloc
        # Simple heuristic: remove 'canvas.' or '.instructure.com'
        if "instructure.com" in domain:
            subdomain = domain.replace(".instructure.com", "")
            if subdomain == "canvas":
                university_name = "Canvas (Global)"
            else:
                university_name = subdomain.capitalize() + " (Instructure)"
        else:
            university_name = domain.replace("canvas.", "")
            
        return university_name, user_name
        
    except InvalidAccessToken:
        raise
    except Exception:
        raise
import requests

def search_canvas_institution(query: str) -> list[dict]:
    """
    Searches for Canvas institutions by name.
    Returns a list of dicts with 'name' and 'domain'.
    """
    try:
        url = "https://canvas.instructure.com/api/v1/accounts/search"
        response = requests.get(url, params={"name": query})
        response.raise_for_status()
        
        data = response.json()
        results = []
        for account in data:
            results.append({
                "name": account.get("name"),
                "domain": account.get("domain")
            })
        return results
    except Exception:
        return []

from datetime import datetime, timedelta, timezone

def get_upcoming_assignments(token: str, base_url: str) -> list[dict]:
    """
    Fetches assignments due within the next 24 hours.
    Returns a list of dicts with course and assignment details.
    """
    upcoming_homeworks = []
    try:
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
            
        canvas = Canvas(base_url, token)
        user = canvas.get_current_user()
        
        # Get active courses
        courses = user.get_courses(enrollment_state='active')
        
        now = datetime.now(timezone.utc)
        limit = now + timedelta(hours=24)
        
        for course in courses:
            # simple check to ensure it's a valid course object with name
            if not hasattr(course, 'name'):
                continue
                
            # Get assignments with submission data
            assignments = course.get_assignments(bucket='upcoming', include=['submission'])
            
            for bond in assignments:
                if not bond.due_at:
                    continue
                    
                # Canvas due_at is ISO8601 string, e.g. 2026-01-29T10:00:00Z
                try:
                    due_date = datetime.fromisoformat(bond.due_at.replace('Z', '+00:00'))
                except ValueError:
                    continue
                
                # Check if due within next 24 hours and strictly in the future
                if now < due_date <= limit:
                    # Prefer SIS ID if available, else course_code
                    code_val = getattr(course, 'sis_course_id', None) or getattr(course, 'course_code', 'Unknown Code')
                    
                    if hasattr(bond, 'submission'):
                        sub = bond.submission
                        state = sub.get('workflow_state') if isinstance(sub, dict) else getattr(sub, 'workflow_state', 'unsubmitted')
                        is_really_submitted = (state == 'submitted')
                    else:
                        is_really_submitted = getattr(bond, 'has_submitted_submissions', False)

                    upcoming_homeworks.append({
                        "assignment_id": bond.id,
                        "course_name": course.name,
                        "course_code": code_val,
                        "homework_name": bond.name,
                        "deadline": bond.due_at, # Keep original string for storage
                        "submission_types": bond.submission_types,
                        "is_submitted": is_really_submitted
                    })
                    
        return upcoming_homeworks
        
        return upcoming_homeworks
        
    except InvalidAccessToken:
        raise
    except Exception:
        return []
