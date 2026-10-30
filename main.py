import os
import json
import requests
import time
from datetime import datetime, timezone
from enum import Enum
import urllib.parse
import jwt  # pyjwt[crypto]
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

GARMIN_URL = os.environ.get("GARMIN_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CREDENTIALS")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")

HEUTE = datetime.now().date()
SYNC_TAG = "[managed-by-garmin-sync]"

class ActionType(str, Enum):
    KEEP = "KEEP"
    UPDATE = "UPDATE"
    CREATE = "CREATE"
    DELETE = "DELETE"

class ReconciledEvent(BaseModel):
    action: ActionType = Field(description="KEEP (no conflict), UPDATE (needs reschedule), CREATE (new), DELETE (cancelled)")
    event_id: str | None = Field(default=None, description="Google Calendar event ID if action is UPDATE or DELETE")
    summary: str = Field(description="Name of the workout")
    start: str | None = Field(default=None, description="ISO 8601 start timestamp with timezone offset")
    end: str | None = Field(default=None, description="ISO 8601 end timestamp with timezone offset")
    reason: str = Field(description="Brief explanation of why this action was chosen")

def get_garmin_data():
    print("Fetching Garmin iCal data...")
    if not GARMIN_URL:
        raise ValueError("GARMIN_URL secret is missing!")
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(GARMIN_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text

def get_google_access_token(info):
    print("Generating Google OAuth2 Access Token...")
    iat = int(time.time())
    exp = iat + 3600
    token_url = "https://oauth2.googleapis.com/token"
    
    payload = {
        "iss": info["client_email"],
        "scope": "https://www.googleapis.com/auth/calendar",
        "aud": token_url,
        "iat": iat,
        "exp": exp
    }
    
    private_key = info["private_key"]
    if isinstance(private_key, str):
        private_key = private_key.replace("\\n", "\n")
        
    signed_jwt = jwt.encode(payload, private_key, algorithm="RS256")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed_jwt
    }
    
    res = requests.post(token_url, headers=headers, data=data, timeout=20)
    res.raise_for_status()
    return res.json()["access_token"]

def get_calendar_events(access_token):
    """Separates hard user meetings from previously AI-managed workouts."""
    print("Fetching existing calendar events for the next 7 days...")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    now = datetime.now(timezone.utc).isoformat()
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events"
    params = {
        "timeMin": now,
        "maxResults": 150,
        "singleEvents": "true",
        "orderBy": "startTime"
    }
    
    response = requests.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()
    items = response.json().get("items", [])
    
    hard_commitments = []
    managed_workouts = []
    
    for item in items:
        desc = item.get("description", "")
        event_info = {
            "id": item["id"],
            "summary": item.get("summary", "Untitled"),
            "start": (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date"),
            "end": (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date"),
        }
        
        if SYNC_TAG in desc:
            managed_workouts.append(event_info)
        else:
            hard_commitments.append(event_info)
            
    return hard_commitments, managed_workouts

def reconcile_with_gemini(garmin_ical, hard_commitments, managed_workouts):
    """Gemini evaluates conflicts, keeps valid workouts, and reschedules conflicting ones."""
    print("Asking Gemini to reconcile and reschedule workouts...")
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are a calendar reconciliation assistant.
    Today's date is: {HEUTE}.
    Default Timezone: Europe/Berlin.

    INPUT DATA:
    1. Garmin Planned Workouts (Source of Truth):
    {garmin_ical}

    2. Hard Commitments (User meetings/appointments - STRICT NO OVERLAP):
    {json.dumps(hard_commitments, indent=2)}

    3. Managed Workouts (Currently placed workouts from previous sync runs):
    {json.dumps(managed_workouts, indent=2)}

    RULES:
    1. STABILITY FIRST: If a Managed Workout does NOT conflict with any Hard Commitment, return action="KEEP". Do NOT move events around without a good reason.
    2. CONFLICT RESOLUTION: If a Managed Workout now overlaps with a Hard Commitment (or violates a 30-minute buffer before/after), return action="UPDATE", keep its 'event_id', and move it to a free, realistic time slot on that same day (e.g. morning 06:30-08:00 or evening 17:30-20:00).
    3. NEW WORKOUTS: If a workout appears in Garmin iCal that is NOT yet in Managed Workouts, return action="CREATE" (event_id=None).
    4. CANCELLATIONS: If a Managed Workout is no longer in the Garmin iCal, return action="DELETE" with its 'event_id'.
    5. Always format start/end as valid ISO 8601 strings with timezone offset (e.g., +01:00 or +02:00).
    """

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[ReconciledEvent]
        ),
    )
    return json.loads(response.text)

def apply_sync_actions(access_token, plan):
    """Executes POST, PATCH, or DELETE on Google Calendar API."""
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    base_url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    for item in plan:
        action = item.get("action")
        title = item.get("summary", "Garmin Workout")
        event_id = item.get("event_id")
        reason = item.get("reason", "")
        
        if action == "KEEP":
            print(f"[KEEP] '{title}' has no conflicts. Leaving intact.")
            
        elif action == "UPDATE" and event_id:
            print(f"[RESCHEDULING] '{title}' to {item['start']} (Reason: {reason})")
            patch_url = f"{base_url}/{event_id}"
            body = {
                "start": {"dateTime": item["start"]},
                "end": {"dateTime": item["end"]}
            }
            res = requests.patch(patch_url, headers=headers, json=body, timeout=20)
            if res.status_code not in (200, 201):
                print(f"Failed to update {title}: {res.text}")
                
        elif action == "CREATE":
            print(f"[CREATING] '{title}' at {item['start']} (Reason: {reason})")
            body = {
                "summary": title,
                "start": {"dateTime": item["start"]},
                "end": {"dateTime": item["end"]},
                "description": f"Scheduled by AI based on Garmin Coach.\n{SYNC_TAG}"
            }
            res = requests.post(base_url, headers=headers, json=body, timeout=20)
            if res.status_code not in (200, 201):
                print(f"Failed to create {title}: {res.text}")
                
        elif action == "DELETE" and event_id:
            print(f"[DELETING] Outdated workout '{title}' (Reason: {reason})")
            del_url = f"{base_url}/{event_id}"
            res = requests.delete(del_url, headers=headers, timeout=20)
            if res.status_code not in (200, 204):
                print(f"Failed to delete {title}: {res.text}")

def main():
    try:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError("GOOGLE_CREDENTIALS secret missing!")
        
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        garmin_ical = get_garmin_data()
        
        access_token = get_google_access_token(info)
        hard_commitments, managed_workouts = get_calendar_events(access_token)
        
        print(f"Found {len(hard_commitments)} user meetings and {len(managed_workouts)} existing Garmin workouts.")
        
        plan = reconcile_with_gemini(garmin_ical, hard_commitments, managed_workouts)
        apply_sync_actions(access_token, plan)
        
        print("Reconciliation and dynamic sync completed successfully!")
    except Exception as e:
        print(f"Sync failed with error: {e}")
        raise e

if __name__ == "__main__":
    main()