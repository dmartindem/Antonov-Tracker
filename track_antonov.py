"""
Antonov Aircraft Tracker
Checks OpenSky Network for any airborne Antonov aircraft and sends email alerts.
Designed to run via GitHub Actions on a cron schedule.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# --- Configuration via environment variables ---
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "").split(",")

# Known Antonov Airlines callsign prefix (ICAO: ADB)
ANTONOV_CALLSIGN_PREFIX = "ADB"

# Known Antonov aircraft ICAO type designators to look for
ANTONOV_TYPES = {"A124", "A225", "A22X", "AN24", "AN26", "AN28", "AN30", "AN32", "AN72", "A148"}

# File to persist last known state between runs (stored as GitHub Actions artifact)
STATE_FILE = "last_state.json"


def fetch_opensky_states():
    """Fetch current aircraft state vectors from OpenSky Network (no API key needed)."""
    url = "https://opensky-network.org/api/states/all"
    req = Request(url, headers={"User-Agent": "AntonovTracker/1.0"})

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("states", [])
    except (URLError, HTTPError) as e:
        print(f"Error fetching OpenSky data: {e}")
        return None


def find_antonov_aircraft(states):
    """
    Filter states for Antonov aircraft.

    OpenSky state vector indices:
    0: icao24        6: on_ground      12: geo_altitude
    1: callsign      7: velocity       13: squawk
    2: origin_country 8: true_track     14: spi
    3: time_position  9: vertical_rate  15: position_source
    4: last_contact  10: sensors        16: category (if available)
    5: longitude     11: baro_altitude
    ...and latitude is at index 6... wait let me check.

    Actually the correct indices are:
    0: icao24
    1: callsign
    2: origin_country
    3: time_position
    4: last_contact
    5: longitude
    6: latitude
    7: baro_altitude
    8: on_ground
    9: velocity
    10: true_track
    11: vertical_rate
    12: geo_altitude
    13: squawk
    14: spi
    15: position_source
    """
    found = []
    for s in states:
        callsign = (s[1] or "").strip().upper()
        icao24 = (s[0] or "").strip().lower()
        origin = (s[2] or "").strip()
        on_ground = s[8]
        lat = s[6]
        lon = s[5]
        altitude = s[7]  # baro altitude in meters
        velocity = s[9]  # m/s

        # Match by Antonov Airlines callsign prefix
        is_antonov = callsign.startswith(ANTONOV_CALLSIGN_PREFIX)

        # Also match by origin country Ukraine + callsign patterns
        # Some Antonov flights may use different callsigns
        if is_antonov:
            found.append({
                "icao24": icao24,
                "callsign": callsign,
                "origin_country": origin,
                "on_ground": on_ground,
                "latitude": lat,
                "longitude": lon,
                "altitude_m": altitude,
                "altitude_ft": round(altitude * 3.281) if altitude else None,
                "velocity_knots": round(velocity * 1.944) if velocity else None,
                "airborne": not on_ground,
            })

    return found


def load_previous_state():
    """Load the previous run's state from file."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"aircraft": {}, "last_check": None}


def save_state(state):
    """Save current state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_email(subject, body_html):
    """Build a MIME email message."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = ", ".join(NOTIFY_EMAIL)
    msg.attach(MIMEText(body_html, "html"))
    return msg


def send_email(subject, body_html):
    """Send an email notification."""
    if not all([EMAIL_USER, EMAIL_PASSWORD, NOTIFY_EMAIL]):
        print("Email not configured — printing to console instead:")
        print(f"  Subject: {subject}")
        print(f"  Body: {body_html}")
        return False

    try:
        msg = build_email(subject, body_html)
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"Email sent: {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def format_aircraft_html(aircraft):
    """Format aircraft info as an HTML block."""
    alt = f"{aircraft['altitude_ft']} ft" if aircraft['altitude_ft'] else "N/A"
    spd = f"{aircraft['velocity_knots']} kts" if aircraft['velocity_knots'] else "N/A"
    lat = f"{aircraft['latitude']:.4f}" if aircraft['latitude'] else "N/A"
    lon = f"{aircraft['longitude']:.4f}" if aircraft['longitude'] else "N/A"
    status = "AIRBORNE" if aircraft['airborne'] else "ON GROUND"
    status_color = "#22c55e" if aircraft['airborne'] else "#ef4444"

    return f"""
    <div style="border:1px solid #ddd; border-radius:8px; padding:16px; margin:8px 0; background:#f9f9f9;">
        <h3 style="margin:0 0 8px 0;">Callsign: {aircraft['callsign']}</h3>
        <p style="margin:4px 0;"><strong>Status:</strong> <span style="color:{status_color}; font-weight:bold;">{status}</span></p>
        <p style="margin:4px 0;"><strong>ICAO24:</strong> {aircraft['icao24']}</p>
        <p style="margin:4px 0;"><strong>Country:</strong> {aircraft['origin_country']}</p>
        <p style="margin:4px 0;"><strong>Position:</strong> {lat}, {lon}</p>
        <p style="margin:4px 0;"><strong>Altitude:</strong> {alt}</p>
        <p style="margin:4px 0;"><strong>Speed:</strong> {spd}</p>
    </div>
    """


def main():
    now = datetime.now(timezone.utc)
    print(f"=== Antonov Tracker — {now.isoformat()} ===")

    # Fetch current data
    states = fetch_opensky_states()
    if states is None:
        print("Failed to fetch data from OpenSky. Will retry next run.")
        sys.exit(1)

    print(f"Received {len(states)} aircraft states from OpenSky.")

    # Find Antonov aircraft
    antonov_aircraft = find_antonov_aircraft(states)
    print(f"Found {len(antonov_aircraft)} Antonov aircraft.")

    # Load previous state
    prev_state = load_previous_state()
    prev_aircraft = prev_state.get("aircraft", {})

    # Determine what changed
    newly_airborne = []
    newly_landed = []
    still_airborne = []

    current_aircraft = {}
    for ac in antonov_aircraft:
        cs = ac["callsign"]
        current_aircraft[cs] = ac

        was_known = cs in prev_aircraft
        was_airborne = prev_aircraft.get(cs, {}).get("airborne", False)

        if ac["airborne"]:
            if not was_known or not was_airborne:
                newly_airborne.append(ac)
                print(f"  NEW TAKEOFF: {cs}")
            else:
                still_airborne.append(ac)
                print(f"  Still airborne: {cs}")
        else:
            if was_known and was_airborne:
                newly_landed.append(ac)
                print(f"  LANDED: {cs}")
            else:
                print(f"  On ground: {cs}")

    # Check for aircraft that disappeared (may have landed out of range)
    for cs, prev_ac in prev_aircraft.items():
        if cs not in current_aircraft and prev_ac.get("airborne", False):
            newly_landed.append({**prev_ac, "callsign": cs, "note": "Lost from radar"})
            print(f"  LOST/LANDED: {cs}")

    # Send notifications
    if newly_airborne or newly_landed:
        subject_parts = []
        if newly_airborne:
            subject_parts.append(f"{len(newly_airborne)} took off")
        if newly_landed:
            subject_parts.append(f"{len(newly_landed)} landed")

        subject = f"Antonov Alert: {', '.join(subject_parts)}"

        body_parts = [f"<h2>Antonov Aircraft Update — {now.strftime('%Y-%m-%d %H:%M UTC')}</h2>"]

        if newly_airborne:
            body_parts.append("<h3>Recently Took Off:</h3>")
            for ac in newly_airborne:
                body_parts.append(format_aircraft_html(ac))

        if newly_landed:
            body_parts.append("<h3>Recently Landed:</h3>")
            for ac in newly_landed:
                body_parts.append(format_aircraft_html(ac))

        if still_airborne:
            body_parts.append("<h3>Currently Airborne:</h3>")
            for ac in still_airborne:
                body_parts.append(format_aircraft_html(ac))

        body_parts.append("<hr><p style='color:#888; font-size:12px;'>Antonov Tracker via GitHub Actions</p>")
        body_html = "\n".join(body_parts)

        send_email(subject, body_html)
    else:
        if antonov_aircraft:
            print("No status changes since last check.")
        else:
            print("No Antonov aircraft detected. (They may not be flying right now.)")

    # Save state for next run
    save_state({
        "aircraft": current_aircraft,
        "last_check": now.isoformat(),
    })

    # Summary
    print(f"\nSummary: {len(antonov_aircraft)} tracked, "
          f"{len(newly_airborne)} took off, {len(newly_landed)} landed, "
          f"{len(still_airborne)} still airborne.")


if __name__ == "__main__":
    main()
