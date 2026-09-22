import os
import sqlite3
import uuid
import io
import base64
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

import jwt
import qrcode
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "development-secret-change-me")
JWT_SECRET = os.getenv("JWT_SECRET", "development-jwt-secret-change-me")
ORGANIZER_KEY = os.getenv("ORGANIZER_KEY", "organizer123")
DB_FILE = "events.db"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            venue TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            capacity INTEGER NOT NULL CHECK(capacity > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            token_jti TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'REGISTERED',
            checked_in_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(event_id) REFERENCES events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_jti ON tickets(token_jti);
    """)

    if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0:
        conn.executemany("""
            INSERT INTO events
            (name, description, venue, starts_at, capacity)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("TechFest 2026", "Campus technology showcase, demos and competitions.",
             "Main Auditorium", "2026-10-10T10:00", 250),
            ("Cultural Night 2026", "Music, dance and student performances.",
             "Open-Air Theatre", "2026-10-18T18:00", 500),
            ("Hackathon Kickoff", "24-hour campus hackathon opening session.",
             "Innovation Lab", "2026-11-02T09:00", 120)
        ])
    conn.commit()
    conn.close()


def create_ticket_token(ticket, event):
    now = datetime.now(timezone.utc)
    payload = {
        "type": "EVENT_TICKET",
        "ticketId": ticket["id"],
        "eventId": event["id"],
        "eventName": event["name"],
        "attendee": ticket["name"],
        "iss": "campus-event-ticketing",
        "aud": "event-checkin",
        "iat": now,
        "exp": int(now.timestamp()) + 30 * 24 * 60 * 60,
        "jti": ticket["token_jti"],
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def make_qr_data_url(token):
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=8,
        border=3
    )
    qr.add_data(token)
    qr.make(fit=True)
    image = qr.make_image()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


def send_ticket_email(ticket, event, qr_data_url):
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return False

    msg = EmailMessage()
    msg["Subject"] = f"Your ticket - {event['name']}"
    msg["From"] = os.getenv("MAIL_FROM", "Campus Events <tickets@example.com>")
    msg["To"] = ticket["email"]

    msg.set_content(
        f"Your registration for {event['name']} is confirmed. "
        "Open the HTML version to see your QR ticket."
    )

    html = f"""
    <html><body style="font-family:Arial;background:#f4f7fb;padding:30px">
    <div style="max-width:620px;margin:auto;background:white;padding:30px;border-radius:16px">
    <h1>Your event ticket is confirmed 🎟️</h1>
    <p>Hi <strong>{ticket['name']}</strong>,</p>
    <p>Your registration for <strong>{event['name']}</strong> is confirmed.</p>
    <p><strong>Venue:</strong> {event['venue']}</p>
    <p><strong>Start:</strong> {event['starts_at']}</p>
    <div style="text-align:center">
      <img src="{qr_data_url}" width="300" height="300" alt="QR ticket">
    </div>
    <p>Show this QR code at the entrance. A ticket can only be checked in once.</p>
    </div></body></html>
    """
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        username = os.getenv("SMTP_USERNAME", "")
        if username:
            smtp.login(username, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(msg)

    return True


def get_event(event_id):
    conn = get_db()
    event = conn.execute("""
        SELECT e.*,
        (SELECT COUNT(*) FROM tickets t WHERE t.event_id=e.id) AS registered
        FROM events e WHERE e.id=?
    """, (event_id,)).fetchone()
    conn.close()
    return event


@app.get("/")
def home():
    conn = get_db()
    events = conn.execute("""
        SELECT e.*,
        (SELECT COUNT(*) FROM tickets t WHERE t.event_id=e.id) AS registered
        FROM events e ORDER BY starts_at
    """).fetchall()
    conn.close()
    return render_template("index.html", events=events)


@app.get("/events/<int:event_id>")
def event_page(event_id):
    event = get_event(event_id)
    if event is None:
        return "Event not found", 404
    return render_template("event.html", event=event, error=None)


@app.post("/events/<int:event_id>/register")
def register(event_id):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()

    if not name or not email or "@" not in email:
        return render_template("event.html", event=get_event(event_id),
                               error="Please enter a valid name and email."), 400

    conn = get_db()
    try:
        # Lock/check/insert as one short transaction.
        conn.execute("BEGIN IMMEDIATE")
        event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

        if event is None:
            conn.rollback()
            return "Event not found", 404

        registered = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE event_id=?", (event_id,)
        ).fetchone()[0]

        if registered >= event["capacity"]:
            conn.rollback()
            return render_template(
                "event.html",
                event=get_event(event_id),
                error="Registration closed - maximum venue capacity reached."
            ), 409

        ticket_id = str(uuid.uuid4())
        token_jti = str(uuid.uuid4())

        conn.execute("""
            INSERT INTO tickets(id,event_id,name,email,token_jti)
            VALUES(?,?,?,?,?)
        """, (ticket_id, event_id, name, email, token_jti))

        conn.commit()
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    token = create_ticket_token(ticket, event)
    qr_data_url = make_qr_data_url(token)

    email_sent = False
    try:
        email_sent = send_ticket_email(ticket, event, qr_data_url)
    except Exception as error:
        print("EMAIL ERROR:", error)

    return render_template(
        "success.html",
        ticket=ticket,
        event=event,
        token=token,
        qr_data_url=qr_data_url,
        email_sent=email_sent
    )


def organizer_required():
    return session.get("organizer") is True


@app.get("/organizer/login")
def organizer_login_page():
    return render_template("organizer_login.html", error=None)


@app.post("/organizer/login")
def organizer_login():
    if request.form.get("key", "") != ORGANIZER_KEY:
        return render_template("organizer_login.html",
                               error="Invalid organizer key."), 401
    session["organizer"] = True
    return redirect(url_for("organizer_page"))


@app.post("/organizer/logout")
def organizer_logout():
    session.clear()
    return redirect(url_for("organizer_login_page"))


@app.get("/organizer")
def organizer_page():
    if not organizer_required():
        return redirect(url_for("organizer_login_page"))

    conn = get_db()
    events = conn.execute("""
        SELECT e.*,
        (SELECT COUNT(*) FROM tickets t WHERE t.event_id=e.id) AS registered,
        (SELECT COUNT(*) FROM tickets t
         WHERE t.event_id=e.id AND t.status='CHECKED-IN') AS checked_in
        FROM events e ORDER BY starts_at
    """).fetchall()
    conn.close()

    return render_template("organizer.html", events=events)


@app.post("/api/check-in")
def check_in():
    if not organizer_required():
        return jsonify({"ok": False, "message": "Organizer authentication required."}), 401

    data = request.get_json(silent=True) or {}
    raw_token = str(data.get("token", "")).strip()

    if not raw_token:
        return jsonify({
            "ok": False,
            "status": "INVALID TICKET",
            "message": "No ticket token supplied."
        }), 400

    try:
        payload = jwt.decode(
            raw_token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer="campus-event-ticketing",
            audience="event-checkin"
        )
    except jwt.InvalidTokenError:
        return jsonify({
            "ok": False,
            "status": "INVALID TICKET",
            "message": "Malformed, expired, or cryptographically invalid ticket."
        }), 400

    if (payload.get("type") != "EVENT_TICKET"
            or not payload.get("ticketId")
            or not payload.get("jti")
            or not payload.get("eventId")):
        return jsonify({
            "ok": False,
            "status": "INVALID TICKET",
            "message": "Invalid ticket payload."
        }), 400

    conn = get_db()

    ticket = conn.execute("""
        SELECT t.*, e.name AS event_name
        FROM tickets t
        JOIN events e ON e.id=t.event_id
        WHERE t.id=? AND t.token_jti=? AND t.event_id=?
    """, (payload["ticketId"], payload["jti"], payload["eventId"])).fetchone()

    if ticket is None:
        conn.close()
        return jsonify({
            "ok": False,
            "status": "INVALID TICKET",
            "message": "Ticket does not exist in the database."
        }), 404

    if ticket["status"] == "CHECKED-IN":
        conn.close()
        return jsonify({
            "ok": False,
            "status": "ALREADY USED",
            "message": "This ticket was already checked in.",
            "attendee": ticket["name"],
            "event": ticket["event_name"],
            "checkedInAt": ticket["checked_in_at"]
        })

    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute("""
        UPDATE tickets
        SET status='CHECKED-IN', checked_in_at=?
        WHERE id=? AND token_jti=? AND status='REGISTERED'
    """, (now, ticket["id"], ticket["token_jti"]))

    conn.commit()

    if cursor.rowcount != 1:
        latest = conn.execute(
            "SELECT * FROM tickets WHERE id=?", (ticket["id"],)
        ).fetchone()
        conn.close()
        return jsonify({
            "ok": False,
            "status": "ALREADY USED",
            "message": "This ticket was already checked in.",
            "attendee": latest["name"],
            "event": ticket["event_name"],
            "checkedInAt": latest["checked_in_at"]
        })

    conn.close()

    return jsonify({
        "ok": True,
        "status": "CHECKED-IN",
        "message": "Entry verified successfully.",
        "attendee": ticket["name"],
        "event": ticket["event_name"],
        "checkedInAt": now
    })


@app.get("/api/events/<int:event_id>/stats")
def event_stats(event_id):
    if not organizer_required():
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db()
    event = conn.execute("""
        SELECT e.*,
        (SELECT COUNT(*) FROM tickets t WHERE t.event_id=e.id) AS registered,
        (SELECT COUNT(*) FROM tickets t
         WHERE t.event_id=e.id AND t.status='CHECKED-IN') AS checked_in
        FROM events e WHERE e.id=?
    """, (event_id,)).fetchone()
    conn.close()

    if event is None:
        return jsonify({"error": "Event not found"}), 404

    return jsonify(dict(event))


if __name__ == "__main__":
    init_db()
    print("Open http://127.0.0.1:5000")
    print("Organizer: http://127.0.0.1:5000/organizer/login")
    app.run(host="127.0.0.1", port=5000, debug=True)
