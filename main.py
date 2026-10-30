import os
import json
import requests
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
import jwt  # <--- Nutzt jetzt das pure 'PyJWT' Paket, KEIN google-auth mehr!

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

def get_google_access_token(info):
    """Generiert ein reines JWT und holt den Token via HTTP POST."""
    print("Generiere sicheren OAuth-Token via PyJWT...")
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
    
    # Signiert den Token direkt mit dem privaten Schlüssel aus dem JSON
    private_key = info['private_key']
    signed_jwt = jwt.encode(payload, private_key, algorithm='RS256')
    
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
        
    return token_data['access_token']

def get_google_calendar_events(access_token):
    """Holt alle Termine der nächsten 7 Tage direkt via HTTP REST-API."""
    print("Verbinde mit Google Calendar (Direkter HTTP Request)...")
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    
    now = datetime.utcnow().isoformat() + 'Z'
    url = f"https://googleapis.com{now}&maxResults=100&singleEvents=true&orderBy=startTime"
    
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    
    events = response.json().get('items', [])
    cleaned_events = []
    for e in events:
        cleaned_events.append({
            "summary": e.get("summary"),
            "start": e.get("start").get("dateTime") or e.get("start").get("date"),
            "end": e.get("end").get("dateTime") or e.get("end").get("date")
        })
    return cleaned_events

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

def add_events_to_google(access_token, current_events, new_events):
    """Trägt Termine via HTTP POST ein, wenn sie nicht schon existieren."""
    existing_titles = [e['summary'].lower() for e in current_events if e.get('summary')]
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    url = "https://googleapis.com"

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
        
        response = requests.post(url, headers=headers, json=event_body)
        response.raise_for_status()
        print(f"Erfolgreich eingetragen: {title} für den {event['start']}")

def main():
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        garmin_ical = get_garmin_data()
        
        access_token = get_google_access_token(info)
        google_events = get_google_calendar_events(access_token)
        
        ki_plan = ask_gemini_for_schedule(garmin_ical, google_events)
        add_events_to_google(access_token, google_events, ki_plan)
        print("Sync-Vorgang erfolgreich beendet!")
    except Exception as e:
        print(f"Fehler während des Syncs: {e}")

if __name__ == "__main__":
    main()
