import os
import json
import requests
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Umgebungsvariablen von GitHub Secrets
GARMIN_URL = os.environ.get("GARMIN_URL")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CREDENTIALS")
# Google GenAI sucht standardmäßig nach 'GEMINI_API_KEY' in den Secrets
os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")

# Zeitfenster definieren (Heute bis in 7 Tagen)
HEUTE = datetime.now().date()
IN_EINER_WOCHE = HEUTE + timedelta(days=7)

def get_garmin_data():
    """Lädt die statische iCal-Datei von Garmin herunter."""
    print("Lade Garmin iCal-Daten...")
    response = requests.get(GARMIN_URL)
    response.raise_for_status()
    return response.text

def get_google_calendar_events():
    """Verbindet sich mit Google und holt alle Termine der nächsten 7 Tage."""
    print("Verbinde mit Google Calendar...")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=['https://googleapis.com'])
    service = build('calendar', 'v3', credentials=creds)
    
    # Ab jetzt (ISO-Format mit Zeitzone)
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
    
    # Strukturierte JSON-Ausgabe erzwingen
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
    # Erstelle eine Liste aller bestehenden Titel zur schnellen Prüfung
    existing_titles = [e['summary'].lower() for e in current_events if e.get('summary')]

    for event in new_events:
        title = event.get('summary', 'Garmin Workout')
        
        # Duplikats-Prüfung: Wenn das Training vom Namen her schon im Kalender steht, überspringen
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
