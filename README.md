# Campus Event Ticketing System — Python/Flask

## Run
python -m venv venv
# macOS/Linux: source venv/bin/activate
# Windows: venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
python app.py

Open http://127.0.0.1:5000
Organizer: http://127.0.0.1:5000/organizer/login
Default development organizer key: organizer123

Configure SMTP in .env for real confirmation emails. Without SMTP, the confirmation page exposes a developer testing token.

## Main technologies
Flask = backend/web server
SQLite = database
PyJWT = signed ticket tokens
qrcode = QR generation
smtplib = transactional email
html5-qrcode = webcam scanning in browser

## Security
JWTs are signed and verified. Tickets are also checked against the database. Check-in uses a conditional UPDATE from REGISTERED to CHECKED-IN so concurrent scans cannot both become the first successful scan.
