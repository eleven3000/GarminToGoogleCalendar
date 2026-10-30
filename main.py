import os
import json
import requests
import time
from datetime import datetime, timezone
import jwt  # pyjwt[crypto]
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Environment variables
GARMIN_URL = os.environ.get("GARMIN_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CREDENTIALS")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Your Gmail / Google Calendar ID (Defaults to primary if not set)
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")

HEUTE = datetime.now().date()

# Schema for Gemini structured output
class ScheduledWorkout(BaseModel):
    summary: str = Field(description="Name/Title of the workout")
    start: str = Field(description="ISO 8601 start timestamp, e.g. 2026-10-30T07:00:00+01:00")
    end: str = Field(description="ISO 8601 end timestamp, e.g. 2026-10-30T08:00:00+01:00")
    description: str = Field(description="Details or instructions for the workout")

def get_garmin_data():
    """Downloads Garmin iCal file."""
    print("Fetching Garmin iCal data...")
    if not GARMIN_URL:
        raise ValueError("GARMIN_URL secret is not set!")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    response = requests.get(GARMIN_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text

def get_google_access_token(info):
    """Generates OAuth2 token for Google API."""
    print("Generating Google OAuth2 Access Token via PyJWT...")
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
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed_jwt
    }
    
    token_response = requests.post(token_url, headers=headers, data=data, timeout=20)
    if token_response.status_code != 200:
        raise Exception(f"Google Token request failed: {token_response.status_code} - {token_response.text}")
        
    token_data = token_response.json()
    return token_data["access_token"]

def get_google_calendar_events(access_token):
    """Fetches existing events for the next 7 days."""
    print(f"Connecting to Google Calendar ({CALENDAR_ID})...")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    
    now = datetime.now(timezone.utc).isoformat()
    url = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events"
    params = {
        "timeMin": now,
        "maxResults": 100,
        "singleEvents": "true",
        "orderBy": "startTime"
    }
    
    response = requests.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()
    
    events = response.json().get("items", [])
    cleaned_events = []
    for e in events:
        cleaned_events.append({
            "summary": e.get("summary", "No title"),
            "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")
        })
    return cleaned_events

def ask_gemini_for_schedule(garmin_ical, google_events):
    """Uses Gemini to find conflict-free workout slots."""
    print("Asking Gemini for schedule...")
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are a personal fitness scheduling assistant.
    Today's date is: {HEUTE}.
    Timezone: Europe/Berlin.
    
    Task:
    Read the upcoming workouts from the Garmin iCal feed and schedule them into my Google Calendar for the next 7 days.
    
    Rules:
    1. NEVER overlap workouts with existing Google Calendar events.
    2. Add 30 minutes of buffer before and after workouts for changing/showering.
    3. Schedule realistic times (e.g. 06:30 - 08:00 before work, or 17:30 - 20:00 after work, flexible on weekends).
    4. Provide valid ISO 8601 datetimes with offset (e.g. "+01:00" or "+02:00").
    
    Existing Google Calendar Events:
    {json.dumps(google_events, indent=2)}

    Garmin iCal feed:
    {garmin_ical}
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[ScheduledWorkout]
        ),
    )
    
    return json.loads(response.text)

def add_events_to_google(access_token, current_events, new_events):
    """Inserts non-duplicate events into Google Calendar."""
    existing_titles = [e["summary"].lower() for e in current_events if e.get("summary")]
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    url = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events"

    for event in new_events:
        title = event.get("summary", "Garmin Workout")
        
        if title.lower() in existing_titles:
            print(f"Skipping (already exists): {title}")
            continue
            
        event_body = {
            "summary": title,
            "start": {"dateTime": event["start"]},
            "end": {"dateTime": event["end"]},
            "description": event.get("description", "Scheduled by AI based on Garmin Coach.")
        }
        
        res = requests.post(url, headers=headers, json=event_body, timeout=20)
        if res.status_code not in (200, 201):
            print(f"Failed to add {title}: {res.status_code} - {res.text}")
        else:
            print(f"Successfully added: {title} at {event['start']}")

def main():
    try:
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError("GOOGLE_CREDENTIALS secret is missing!")
            
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        garmin_ical = get_garmin_data()
        
        access_token = get_google_access_token(info)
        google_events = get_google_calendar_events(access_token)
        
        ki_plan = ask_gemini_for_schedule(garmin_ical, google_events)
        print(f"Generated {len(ki_plan)} planned workout(s).")
        
        add_events_to_google(access_token, google_events, ki_plan)
        print("Sync completed successfully!")
    except Exception as e:
        print(f"Sync failed with error: {e}")
        raise e

if __name__ == "__main__":
    main()