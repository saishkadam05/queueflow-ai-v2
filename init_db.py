"""
Run this ONCE after setting up your Neon Postgres database and env vars,
to create all tables. From your project folder:

    python init_db.py

You'll need DATABASE_URL, SECRET_KEY, and RESEND_API_KEY set in your
environment (or in a local .env file) before running this.
"""

from app import app, db

with app.app_context():
    db.create_all()
    print("Tables created successfully.")
