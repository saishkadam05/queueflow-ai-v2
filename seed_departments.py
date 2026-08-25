"""
Run this ONCE after init_db.py, to create the starting departments.
Safe to re-run — it skips any department that already exists by name.

    python seed_departments.py
"""

from app import app, db, Department

DEPARTMENTS = [
    ("Hospital", "Medical consultations and treatment"),
    ("Bank", "Banking and financial services"),
    ("College", "Student admissions and administration"),
    ("Government Office", "Public services and documentation"),
]

with app.app_context():
    for name, description in DEPARTMENTS:
        existing = Department.query.filter_by(name=name).first()
        if existing:
            print(f"Skipped (already exists): {name}")
            continue

        db.session.add(Department(name=name, description=description))
        print(f"Created: {name}")

    db.session.commit()
    print("Done.")
