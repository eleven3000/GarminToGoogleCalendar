import os
import json
import requests
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.auth import jwt  # Nutzt JWT statt des verbuggten Refresh-Mechanismus
from googleapiclient.discovery import build

# Umgebungsvariablen von GitHub Secrets
GARMIN_URL = os.environ.get("GARMIN_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CREDENTIALS")
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")

HEUTE = datetime.now().date()

def get_garmin_data():
    """Lädt die iCal-Datei von Garmin herunter."""
    print("Lade Garmin iCal-Daten...")
    response = requests.get(GARMIN_URL)
    response.raise_for_status()
    return response.text

def get_google_calendar_events():
    """Holt den Access Token manuell über ein signiertes JWT und baut die Credentials sauber auf."""
    print("Verbinde mit Google Calendar (Manueller JWT-Auth)...")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    
    # 1. Wir bauen den JWT-Claim manuell auf
    iat = int(time.time())
    exp = iat + 3600
    payload = {
        'iss': info['client_email'],
        'sub': info['client_email'],
        'scope': 'https://googleapis.com',
        'aud': 'https://googleapis.com',
        'iat': iat,
        'exp': exp
    }
    
    # 2. Signieren des Tokens mit dem Private Key aus dem Secret
    signer = jwt.Credentials.from_service_account_info(info)
    signed_jwt = jwt.encode(signer._signer, payload)
    
    # 3. Den echten Access Token direkt via HTTP POST abholen
    token_response = requests.post(
        'https://googleapis.com',
        data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': signed_jwt
        }
    )
    
    token_data = token_response.json()
    if 'access_token' not in token_data:
        raise Exception(f"Google OAuth fehlgeschlagen: {token_data}")
        
    access_token = token_data['access_token']
    
    # 4. FIX: Das Credentials-Objekt mit allen benötigten Attributen für 'build' befüllen
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=access_token,
        token_uri='https://googleapis.com',
        client_id=info.get('client_id'),
        scopes=['https://googleapis.com']
    )
    
    service = build('calendar', 'v3', credentials=creds)
    
    now = datetime.utcnow().isoformat() + 'Z'
    events_result = service.events().list(
        calendarId='primary', timeMin=now,
        maxResults=100, singleEvents=True, orderBy='startTime'
    ).execute()
    
    events = events_result.get('items', [])
    cleaned_events = []
    for e in events:
        cleaned_events.append({
            "summary": e.get("summary"),
            "start": e.get("start").get("dateTime") or e.get("start").get("date"),
            "end": e.get("end").get("dateTime") or e.get("end").get("date")
        })
    return service, cleaned_events

def ask_gemini_for_schedule(garmin_ical, google_events):
    """Nutzt das kostenlose Gemini 1.5 Flash Modell für die smarte Planung."""
    print("Frage Gemini nach optimalen Zeitfenstern...")
    client = genai.Client()
    
    prompt = f"""
    Du bist ein persönlicher Fitness-Assistent. Ich gebe dir meine anstehenden Garmin-Workouts (iCal-Format) und meinen aktuellen Google-Kalender der nächsten 7 Tage.
    Verplane die Garmin-Workouts optimal in meinen Google-Kalender.
    
    Regeln:
    1. Plane Workouts NIEMALS in bereits bestehende Termine ein.
    2. Halte vor und nach jedem Workout mindestens 30 Minuten Puffer (für Duschen/Umziehen).
    3. Beachte das heutige Datum: {HEUTE}.
    4. Setze realistische Uhrzeiten (z.B. morgens vor der Arbeit oder abends im Feierabend, außer am Wochenende).
    
    Google Kalender Termine:
    {json.dumps(google_events, indent=2)}

    Garmin iCal Daten:
    {garmin_ical}
    """
    
    response = client.models.generate_content(
        model='gemini-1.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[dict]
        ),
    )
    
    return json.loads(response.text)

def add_events_to_google(service, current_events, new_events):
    """Trägt Termine nur ein, wenn sie nicht schon existieren (Duplikats-Schutz)."""
    existing_titles = [e['summary'].lower() for e in current_events if e.get('summary')]

    for event in new_events:
        title = event.get('summary', 'Garmin Workout')
        
        if title.lower() in existing_titles:
            print(f"Übersprungen (bereits eingetragen): {title}")
            continue
            
        event_body = {
            'summary': title,
            'start': {'dateTime': event['start'], 'timeZone': 'Europe/Berlin'},
            'end': {'dateTime': event['end'], 'timeZone': 'Europe/Berlin'},
            'description': 'Automatisch von KI eingeplant auf Basis deines Garmin Coach Plans.'
        }
        
        service.events().insert(calendarId='primary', body=event_body).execute()
        print(f"Erfolgreich eingetragen: {title} für den {event['start']}")

def main():
    try:
        garmin_ical = get_garmin_data()
        service, google_events = get_google_calendar_events()
        ki_plan = ask_gemini_for_schedule(garmin_ical, google_events)
        add_events_to_google(service, google_events, ki_plan)
        print("Sync-Vorgang erfolgreich beendet!")
    except Exception as e:
        print(f"Fehler während des Syncs: {e}")

if __name__ == "__main__":
    main()
