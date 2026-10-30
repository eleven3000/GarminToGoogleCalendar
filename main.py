import os
import json
import re
import requests
import time
from datetime import datetime, timedelta, timezone
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
    """Fetches Garmin iCal data and filters workouts for the next 7 days only."""
    print("Fetching and filtering Garmin iCal data...")
    if not GARMIN_URL:
        raise ValueError("GARMIN_URL secret is missing!")
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(GARMIN_URL, headers=headers, timeout=30)
    response.raise_for_status()
    
    # Filter the iCal raw feed to only upcoming workouts within 7 days
    raw_text = response.text
    upcoming_workouts = []
    today_str = HEUTE.strftime("%Y%m%d")
    next_week_str = (HEUTE + timedelta(days=8)).strftime("%Y%m%d")
    
    events = raw_text.split("BEGIN:VEVENT")
    for block in events[1:]:
        lines = block.split("\n")
        workout = {}
        for line in lines:
            line = line.strip()
            if line.startswith("SUMMARY:"):
                workout["summary"] = line.replace("SUMMARY:", "").strip()
            elif line.startswith("DTSTART"):
                # Matches YYYYMMDD from DTSTART;VALUE=DATE:20261102 or DTSTART:20261102T...
                date_match = re.search(r'(\d{8})', line)
                if date_match:
                    workout["date"] = date_match.group(1)
            elif line.startswith("DESCRIPTION:"):
                workout["description"] = line.replace("DESCRIPTION:", "").strip()
            elif line.startswith("END:VEVENT"):
                break
                
        # Keep only if it's within the next 7 days
        w_date = workout.get("date", "")
        if today_str <= w_date <= next_week_str and "summary" in workout:
            upcoming_workouts.append(workout)
            
    print(f"Extracted {len(upcoming_workouts)} upcoming Garmin workout(s) for the next 7 days.")
    return upcoming_workouts

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
    """Fetches Google Calendar events strictly for the next 7 days."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    now_dt = datetime.now(timezone.utc)
    time_min = now_dt.isoformat()
    time_max = (now_dt + timedelta(days=7)).isoformat()
    
    print(f"Fetching calendar events from {time_min[:10]} to {time_max[:10]}...")
    encoded_cal = urllib.parse.quote(CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_cal}/events"
    params = {
        "timeMin": time_min,
        "timeMax": time_max,      # Strict 7-day limit
        "maxResults": 100,
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
        start_obj = item.get("start", {})
        end_obj = item.get("end", {})
        
        event_info = {
            "id": item["id"],
            "summary": item.get("summary", "Busy"),
            "start": start_obj.get("dateTime") or start_obj.get("date"),
            "end": end_obj.get("dateTime") or end_obj.get("date"),
        }
        
        if SYNC_TAG in desc:
            managed_workouts.append(event_info)
        else:
            hard_commitments.append(event_info)
            
    return hard_commitments, managed_workouts

def reconcile_with_gemini(upcoming_workouts, hard_commitments, managed_workouts):
    """Gemini evaluates conflicts and schedules slots."""
    print("Asking Gemini to reconcile and reschedule workouts...")
    # Initialize without the 60s hard timeout header
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are a calendar reconciliation assistant.
    Today's date is: {HEUTE}.
    Default Timezone: Europe/Berlin.

    INPUT DATA:
    1. Garmin Planned Workouts for the next 7 days:
    {json.dumps(upcoming_workouts, indent=2)}

    2. Hard Commitments (User meetings/appointments - STRICT NO OVERLAP):
    {json.dumps(hard_commitments, indent=2)}

    3. Managed Workouts (Currently placed workouts from previous sync runs):
    {json.dumps(managed_workouts, indent=2)}

    RULES:
    1. STABILITY FIRST: If a Managed Workout does NOT conflict with any Hard Commitment, return action="KEEP".
    2. CONFLICT RESOLUTION: If a Managed Workout overlaps with a Hard Commitment (or violates 30-min buffer), return action="UPDATE", keep its 'event_id', and reschedule it on that same day (e.g. 06:30-08:00 or 17:30-20:00).
    3. NEW WORKOUTS: For Garmin workouts not yet in Managed Workouts, return action="CREATE" (event_id=None).
    4. CANCELLATIONS: If a Managed Workout is no longer in Garmin Planned Workouts, return action="DELETE" with its 'event_id'.
    5. Always format start/end as valid ISO 8601 strings with timezone offset (e.g., +01:00 or +02:00).
    """

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[ReconciledEvent],
            # Use 'low' thinking to respond in seconds and prevent timeouts
            thinking_config=types.ThinkingConfig(thinking_level="low")
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
        upcoming_workouts = get_garmin_data()
        
        access_token = get_google_access_token(info)
        hard_commitments, managed_workouts = get_calendar_events(access_token)
        
        print(f"Found {len(hard_commitments)} user meetings and {len(managed_workouts)} existing Garmin workouts.")
        
        plan = reconcile_with_gemini(upcoming_workouts, hard_commitments, managed_workouts)
        apply_sync_actions(access_token, plan)
        
        print("Reconciliation and dynamic sync completed successfully!")
    except Exception as e:
        print(f"Sync failed with error: {e}")
        raise e

if __name__ == "__main__":
    main()