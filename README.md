# QueueFlow AI

Smart queue management system built with Flask, PostgreSQL, and Socket.IO for real-time updates.

## Stack

- **Backend:** Flask + Flask-SocketIO (threading mode)
- **Database:** PostgreSQL (SQLAlchemy ORM)
- **Email:** Resend API
- **PDF/QR generation:** ReportLab + qrcode

## Local Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv venv
   venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in real values:

   ```bash
   copy .env.example .env
   ```

3. Create your database tables and seed the starting departments:

   ```bash
   python init_db.py
   python seed_departments.py
   ```

4. Run locally:

   ```bash
   python app.py
   ```

## Environment Variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret. Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | Postgres connection string, e.g. from Neon |
| `RESEND_API_KEY` | API key from resend.com |
| `MAIL_FROM` | Sender shown on outgoing emails, e.g. `QueueFlow AI <onboarding@resend.dev>` |
| `ADMIN_EMAIL` | The account email allowed to access `/admin` |

## Deploying (Render, free tier)

1. Push this project to a GitHub repo (the `.gitignore` already excludes `.env` and cached files).
2. On [render.com](https://render.com), create a New Web Service and connect the repo. Render will detect the `Procfile` automatically.
3. Add all 5 environment variables above under the service's Environment tab.
4. After the first deploy, open the Render Shell tab and run:

   ```bash
   python init_db.py
   python seed_departments.py
   ```

5. Visit your Render URL. Register an account, then set that account's email as `ADMIN_EMAIL` to access `/admin`.

## Notes

- `Procfile` runs a single gunicorn worker with multiple threads. **Do not increase worker count** — `connected_users` (used for Socket.IO routing) is stored in memory and requires a single process to stay consistent.
- `static/qr/` holds generated QR codes and PDFs. On most free hosts this directory is **wiped on every redeploy/restart** — this is expected; files regenerate as users interact with the app, but don't rely on old files persisting.
- Render's free tier spins the service down after ~15 minutes of inactivity; the first request after that will take 30–60 seconds to respond while it wakes up.
