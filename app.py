from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    Table,
    TableStyle
)

from flask import jsonify
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import inch
import qrcode
from pathlib import Path
from flask_socketio import SocketIO
from urllib.parse import quote_plus
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
import resend

PRIORITY_ORDER = {
    "Emergency": 1,
    "Pregnant": 2,
    "Senior Citizen": 3,
    "Disabled": 4,
    "Normal": 5
}

# Load .env file (local dev only — Render/host will inject real env vars directly)
load_dotenv()

app = Flask(__name__)

# SECURITY: secret key now comes from environment, never hardcoded.
app.secret_key = os.environ["SECRET_KEY"]

# ------------------------------------------------------------------
# Email (Resend only — flask_mail/SMTP removed, both send paths now
# use the same helper below so there's only one place to fix things)
# ------------------------------------------------------------------
resend.api_key = os.environ["RESEND_API_KEY"]
MAIL_FROM = os.environ.get("MAIL_FROM", "QueueFlow AI <onboarding@resend.dev>")


def send_email(to_email, subject, text_body):
    """Central email helper. Never lets an email failure crash a request —
    it logs and continues, since email should not block queue operations."""
    try:
        resend.Emails.send({
            "from": MAIL_FROM,
            "to": [to_email],
            "subject": subject,
            "text": text_body
        })
    except Exception as e:
        print(f"[email] failed to send to {to_email}: {e}")


# ------------------------------------------------------------------
# Database Configuration (Postgres via Neon/Supabase)
# ------------------------------------------------------------------
import ssl
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

db_url = os.environ["DATABASE_URL"]
# Render/Neon sometimes hand back "postgres://" — SQLAlchemy needs "postgresql://"
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
# Use the pure-Python pg8000 driver (no compiled dependencies, avoids
# psycopg2's pg_config build issues on newer Python versions)
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)

# pg8000 doesn't understand "sslmode" the way psycopg2 does — strip it from
# the URL and configure SSL via connect_args instead (Neon requires SSL).
parsed = urlparse(db_url)
query = parse_qs(parsed.query)
wants_ssl = query.pop("sslmode", None) is not None
query.pop("channel_binding", None)
db_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

engine_options = {"pool_pre_ping": True}
if wants_ssl:
    ssl_context = ssl.create_default_context()
    engine_options["connect_args"] = {"ssl_context": ssl_context}

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options

db = SQLAlchemy(app)

# async_mode="threading" is intentional: connected_users is an in-memory
# dict, so this app MUST run as a single process/worker (see Procfile).
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)

connected_users = {}


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    queues = db.relationship("Queue", backref="user", lazy=True)


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    queues = db.relationship("Queue", backref="department", lazy=True)


class Queue(db.Model):
    __tablename__ = "queues"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    token = db.Column(db.String(20), nullable=False)
    qr_code = db.Column(db.String(255))
    status = db.Column(db.String(20), default="Waiting")
    priority = db.Column(db.String(20), default="Normal")
    priority_status = db.Column(db.String(20), default="Approved")
    serving_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    user = db.relationship("User", backref="notifications")


def get_average_service_time(department_id):
    completed_queues = Queue.query.filter(
        Queue.department_id == department_id,
        Queue.completed_at.isnot(None),
        Queue.serving_at.isnot(None)
    ).all()

    if not completed_queues:
        return 5  # Default 5 minutes

    total_minutes = 0
    for queue in completed_queues:
        duration = (queue.completed_at - queue.serving_at).total_seconds() / 60
        total_minutes += duration

    return round(total_minutes / len(completed_queues), 1)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["user_name"] = user.fullname
            flash("Login Successful!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid Email or Password!", "danger")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fullname = request.form["fullname"]
        email = request.form["email"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            flash("Passwords do not match!", "danger")
            return redirect(url_for("register"))

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Email already registered!", "danger")
            return redirect(url_for("register"))

        hashed_password = generate_password_hash(password)

        new_user = User(
            fullname=fullname,
            email=email,
            password=hashed_password
        )

        db.session.add(new_user)
        db.session.commit()

        send_email(
            email,
            "Welcome to QueueFlow AI",
            f"""Hello {fullname},

Welcome to QueueFlow AI!

Your account has been created successfully.

You can now log in and start using the queue management system.

Thank you,
QueueFlow AI Team
"""
        )

        flash("Registration successful! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    departments = Department.query.all()

    my_token = Queue.query.filter_by(
        user_id=session["user_id"]
    ).filter(
        Queue.status.in_(["Waiting", "Serving"])
    ).first()

    position = None
    people_ahead = None
    total_waiting = 0
    now_serving = None

    if my_token:
        if my_token.status == "Serving":
            position = 1
            people_ahead = 0
        elif my_token.status == "Waiting":
            position = Queue.query.filter(
                Queue.department_id == my_token.department_id,
                Queue.status == "Waiting",
                Queue.id <= my_token.id
            ).count()
            people_ahead = max(position - 1, 0)

        total_waiting = Queue.query.filter_by(
            department_id=my_token.department_id,
            status="Waiting"
        ).count()

        now_serving = Queue.query.filter_by(
            department_id=my_token.department_id,
            status="Serving"
        ).first()

    notifications = Notification.query.filter_by(
        user_id=session["user_id"]
    ).order_by(
        Notification.created_at.desc()
    ).limit(10).all()

    unread_count = Notification.query.filter_by(
        user_id=session["user_id"],
        is_read=False
    ).count()

    estimated_wait = 0
    avg_service_time = 5
    completed_count = 0
    confidence = 0

    if my_token:
        completed_queues = Queue.query.filter(
            Queue.department_id == my_token.department_id,
            Queue.completed_at.isnot(None),
            Queue.serving_at.isnot(None)
        ).all()

        service_times = []
        for q in completed_queues:
            minutes = (q.completed_at - q.serving_at).total_seconds() / 60
            service_times.append(minutes)

        if service_times:
            avg_service_time = round(sum(service_times) / len(service_times), 1)
        else:
            avg_service_time = 5

        completed_count = len(service_times)
        estimated_wait = round(people_ahead * avg_service_time, 1)
        confidence = min(99, 80 + completed_count)

    return render_template(
        "dashboard.html",
        username=session["user_name"],
        departments=departments,
        my_token=my_token,
        position=position,
        people_ahead=people_ahead,
        total_waiting=total_waiting,
        now_serving=now_serving,
        estimated_wait=estimated_wait,
        avg_service_time=avg_service_time,
        completed_count=completed_count,
        confidence=confidence,
        notifications=notifications,
        unread_count=unread_count
    )


@app.route("/history")
def history():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    queues = Queue.query.filter_by(
        user_id=session["user_id"]
    ).order_by(
        Queue.created_at.desc()
    ).all()

    return render_template("history.html", queues=queues)


@app.route("/clear_notifications")
def clear_notifications():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    Notification.query.filter_by(user_id=session["user_id"]).delete()
    db.session.commit()

    flash("Notifications cleared successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/profile")
def profile():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    user = User.query.get(session["user_id"])

    total_tokens = Queue.query.filter_by(user_id=user.id).count()
    completed_tokens = Queue.query.filter_by(user_id=user.id, status="Completed").count()
    active_tokens = Queue.query.filter(
        Queue.user_id == user.id,
        Queue.status.in_(["Waiting", "Serving"])
    ).count()

    return render_template(
        "profile.html",
        user=user,
        total_tokens=total_tokens,
        completed_tokens=completed_tokens,
        active_tokens=active_tokens
    )


@app.route("/join/<int:department_id>", methods=["POST"])
def join_queue(department_id):
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    department = Department.query.get_or_404(department_id)
    priority = request.form.get("priority", "Normal")

    priority_status = "Approved" if priority == "Normal" else "Pending"

    existing_queue = Queue.query.filter(
        Queue.user_id == session["user_id"],
        Queue.status.in_(["Waiting", "Serving"])
    ).first()

    if existing_queue:
        flash(
            f"You already have an active token ({existing_queue.token}) in {existing_queue.department.name}. Complete it before joining another queue.",
            "warning"
        )
        return redirect(url_for("dashboard"))

    count = Queue.query.filter(
        Queue.department_id == department_id,
        Queue.status.in_(["Waiting", "Serving", "Completed"])
    ).count() + 1

    words = department.name.split()
    if len(words) == 1:
        prefix = words[0][0].upper()
    else:
        prefix = "".join(word[0].upper() for word in words)

    token = f"{prefix}{count:03d}"

    current_serving = Queue.query.filter_by(
        department_id=department_id,
        status="Serving"
    ).first()

    queue_status = "Waiting" if current_serving else "Serving"

    new_queue = Queue(
        user_id=session["user_id"],
        department_id=department_id,
        token=token,
        status=queue_status,
        priority=priority,
        priority_status=priority_status
    )

    if queue_status == "Serving":
        new_queue.serving_at = datetime.utcnow()

    db.session.add(new_queue)
    db.session.commit()

    # NOTE: QR images are saved to local disk. On most free hosts this disk
    # is EPHEMERAL and gets wiped on restart/redeploy. This still works
    # within a single running instance, but don't rely on old QR files
    # surviving a redeploy. See deployment notes for a persistent-storage
    # upgrade path if you need these to survive restarts.
    qr_data = f"""QueueFlow AI

Name: {session['user_name']}
Department: {department.name}
Token: {token}
Status     : {queue_status}
Priority   : {priority}
Approval   : {priority_status}
"""

    img = qrcode.make(qr_data)

    qr_folder = Path("static/qr")
    qr_folder.mkdir(parents=True, exist_ok=True)

    filename = f"{token}.png"
    img.save(qr_folder / filename)

    new_queue.qr_code = filename
    db.session.commit()

    user = User.query.get(session["user_id"])

    send_email(
        user.email,
        "QueueFlow AI - Token Confirmation",
        f"""Hello {user.fullname},

Your queue has been created successfully.

Department : {department.name}
Token      : {token}
Status: {queue_status}
Priority: {priority}
Approval: {priority_status}

Thank you for using QueueFlow AI.
"""
    )

    socketio.emit("queue_updated", {"refresh": True})

    if priority_status == "Pending":
        flash(
            f"Priority request submitted successfully. Your token is {token}. Waiting for admin approval.",
            "warning"
        )
    else:
        flash(f"Successfully joined {department.name}. Your token is {token}.", "success")

    return redirect(url_for("dashboard"))


@app.route("/cancel/<int:queue_id>")
def cancel_queue(queue_id):
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    queue = Queue.query.get_or_404(queue_id)

    if queue.user_id != session["user_id"]:
        flash("Unauthorized access!", "danger")
        return redirect(url_for("dashboard"))

    if queue.status != "Waiting":
        flash("Only waiting tokens can be cancelled.", "warning")
        return redirect(url_for("dashboard"))

    db.session.delete(queue)
    db.session.commit()

    socketio.emit("queue_updated", {"refresh": True})

    flash("Queue cancelled successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin")
def admin():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    user = User.query.get(session["user_id"])

    admin_email = os.environ.get("ADMIN_EMAIL", "")
    if user.email != admin_email:
        flash("Access Denied!", "danger")
        return redirect(url_for("dashboard"))

    total_users = User.query.count()
    active_tokens = Queue.query.filter(Queue.status.in_(["Waiting", "Serving"])).count()
    waiting_tokens = Queue.query.filter_by(status="Waiting").count()
    serving_tokens = Queue.query.filter_by(status="Serving").count()
    completed_tokens = Queue.query.filter_by(status="Completed").count()

    department_stats = []
    departments = Department.query.all()

    status_chart = [waiting_tokens, serving_tokens, completed_tokens]

    department_labels = []
    department_totals = []

    for department in departments:
        waiting = Queue.query.filter_by(department_id=department.id, status="Waiting").count()
        serving = Queue.query.filter_by(department_id=department.id, status="Serving").count()
        completed = Queue.query.filter_by(department_id=department.id, status="Completed").count()

        department_stats.append({
            "name": department.name,
            "waiting": waiting,
            "serving": serving,
            "completed": completed
        })

        department_labels.append(department.name)
        department_totals.append(waiting + serving + completed)

    search = request.args.get("search", "")
    status = request.args.get("status", "")

    query = Queue.query

    if search:
        query = query.join(User).join(Department).filter(
            db.or_(
                Queue.token.ilike(f"%{search}%"),
                User.fullname.ilike(f"%{search}%"),
                Department.name.ilike(f"%{search}%")
            )
        )

    if status:
        query = query.filter(Queue.status == status)

    queues = query.order_by(Queue.created_at.asc()).all()

    pending_requests = Queue.query.filter(
        Queue.priority != "Normal",
        Queue.priority_status == "Pending"
    ).order_by(Queue.created_at.asc()).all()

    return render_template(
        "admin.html",
        queues=queues,
        total_users=total_users,
        active_tokens=active_tokens,
        waiting_tokens=waiting_tokens,
        serving_tokens=serving_tokens,
        completed_tokens=completed_tokens,
        department_stats=department_stats,
        status_chart=status_chart,
        department_labels=department_labels,
        department_totals=department_totals,
        search=search,
        status=status,
        pending_requests=pending_requests
    )


@app.route("/display")
def display():
    departments = Department.query.all()
    display_data = []

    for department in departments:
        serving = Queue.query.filter_by(department_id=department.id, status="Serving").first()
        display_data.append({
            "department": department.name,
            "token": serving.token if serving else "---"
        })

    return render_template("display.html", display_data=display_data)


@app.route("/call/<int:queue_id>")
def call_token(queue_id):
    queue = Queue.query.get_or_404(queue_id)

    current = Queue.query.filter_by(department_id=queue.department_id, status="Serving").first()
    if current:
        current.status = "Completed"

    queue.status = "Serving"
    queue.serving_at = datetime.utcnow()

    notification = Notification(
        user_id=queue.user_id,
        message=f"🎉 It's your turn! Token {queue.token} is now Serving."
    )
    db.session.add(notification)
    db.session.commit()

    socketio.emit("queue_updated")

    sid = connected_users.get(str(queue.user_id))
    if sid:
        socketio.emit(
            "your_turn",
            {"token": queue.token, "department": queue.department.name},
            room=sid
        )

    flash(f"{queue.token} is now being served.", "success")
    return redirect(url_for("admin"))


@app.route("/complete/<int:queue_id>")
def complete_token(queue_id):
    queue = Queue.query.get_or_404(queue_id)
    department_id = queue.department_id

    queue.status = "Completed"
    queue.completed_at = datetime.utcnow()

    notification = Notification(
        user_id=queue.user_id,
        message=f"✅ Token {queue.token} has been completed."
    )
    db.session.add(notification)

    waiting_queues = Queue.query.filter(
        Queue.department_id == department_id,
        Queue.status == "Waiting",
        Queue.priority_status == "Approved"
    ).all()

    waiting_queues.sort(
        key=lambda q: (PRIORITY_ORDER.get(q.priority, 5), q.created_at)
    )

    next_queue = waiting_queues[0] if waiting_queues else None

    if next_queue:
        next_queue.status = "Serving"
        next_queue.serving_at = datetime.utcnow()

        notification = Notification(
            user_id=next_queue.user_id,
            message=f"🎉 It's your turn! Token {next_queue.token} is now Serving."
        )
        db.session.add(notification)

        sid = connected_users.get(str(next_queue.user_id))
        if sid:
            socketio.emit(
                "your_turn",
                {"token": next_queue.token, "department": next_queue.department.name},
                room=sid
            )

    db.session.commit()

    socketio.emit("queue_updated", {"refresh": True})

    flash("Token completed successfully.", "success")
    return redirect(url_for("admin"))


@app.route("/approve_priority/<int:queue_id>")
def approve_priority(queue_id):
    queue = Queue.query.get_or_404(queue_id)
    queue.priority_status = "Approved"
    db.session.commit()

    serving_queue = Queue.query.filter_by(
        department_id=queue.department_id,
        status="Serving"
    ).first()

    if not serving_queue:
        waiting = Queue.query.filter(
            Queue.department_id == queue.department_id,
            Queue.status == "Waiting",
            Queue.priority_status == "Approved"
        ).all()

        waiting.sort(key=lambda q: (PRIORITY_ORDER.get(q.priority, 5), q.created_at))

        if waiting:
            waiting[0].status = "Serving"

        db.session.commit()

    socketio.emit("queue_updated")

    flash(f"Priority approved for {queue.token}.", "success")
    return redirect(url_for("admin"))


@app.route("/reject_priority/<int:queue_id>")
def reject_priority(queue_id):
    queue = Queue.query.get_or_404(queue_id)
    queue.priority = "Normal"
    queue.priority_status = "Approved"
    db.session.commit()

    socketio.emit("queue_updated")

    flash(f"Priority rejected for {queue.token}.", "warning")
    return redirect(url_for("admin"))


@app.route("/download/<int:queue_id>")
def download_token(queue_id):
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    queue = Queue.query.get_or_404(queue_id)

    if queue.user_id != session["user_id"]:
        flash("Unauthorized!", "danger")
        return redirect(url_for("dashboard"))

    from flask import send_file
    from reportlab.lib.units import mm

    filename = f"{queue.token}_{queue.user.fullname.replace(' ', '_')}.pdf"
    pdf_path = f"static/qr/{filename}"

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=(80 * mm, 180 * mm),
        rightMargin=10,
        leftMargin=10,
        topMargin=10,
        bottomMargin=10
    )
    styles = getSampleStyleSheet()

    title = styles["Title"]
    title.alignment = TA_CENTER

    heading = styles["Heading2"]
    heading.alignment = TA_CENTER
    normal = styles["BodyText"]
    normal.alignment = TA_CENTER

    story = []

    story.append(Paragraph("🚀 QueueFlow AI", title))
    story.append(Paragraph("Smart Queue Management System", heading))
    story.append(Spacer(1, 20))

    story.append(Paragraph(f"<font size=40><b>{queue.token}</b></font>", heading))
    story.append(Spacer(1, 20))

    story.append(Paragraph(f"👤 {queue.user.fullname}", normal))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"🏢 {queue.department.name}", normal))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"🟢 {queue.status}", normal))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"📅 {queue.created_at.strftime('%d %b %Y')}", normal))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"🕒 {queue.created_at.strftime('%I:%M %p')}", normal))
    story.append(Spacer(1, 20))
    story.append(Spacer(1, 20))

    story.append(Paragraph("────────────────────────────", normal))
    story.append(Spacer(1, 15))

    story.append(
        Paragraph(
            """
            Please arrive before your turn.<br/>
            Keep this receipt for verification.<br/><br/>
            Thank you for using<br/>
            🚀 QueueFlow AI
            """,
            normal
        )
    )

    qr_path = Path("static/qr") / queue.qr_code

    if qr_path.exists():
        img = Image(str(qr_path), width=3 * inch, height=3 * inch)
        img.hAlign = "CENTER"
        story.append(img)

    doc.build(story)

    return send_file(pdf_path, as_attachment=True)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully!", "success")
    return redirect(url_for("login"))


@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    user = User.query.get(session["user_id"])

    if request.method == "POST":
        current = request.form["current_password"]
        new = request.form["new_password"]
        confirm = request.form["confirm_password"]

        if not check_password_hash(user.password, current):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("change_password"))

        if new != confirm:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("change_password"))

        user.password = generate_password_hash(new)
        db.session.commit()

        flash("Password changed successfully!", "success")
        return redirect(url_for("profile"))

    return render_template("change_password.html")


@app.route("/edit_profile", methods=["GET", "POST"])
def edit_profile():
    if "user_id" not in session:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

    user = User.query.get(session["user_id"])

    if request.method == "POST":
        fullname = request.form["fullname"]
        email = request.form["email"]

        existing = User.query.filter(
            User.email == email,
            User.id != user.id
        ).first()

        if existing:
            flash("Email already exists!", "danger")
            return redirect(url_for("edit_profile"))

        user.fullname = fullname
        user.email = email
        db.session.commit()

        session["user_name"] = fullname

        flash("Profile updated successfully!", "success")
        return redirect(url_for("profile"))

    return render_template("edit_profile.html", user=user)


@app.route("/ask_ai", methods=["POST"])
def ask_ai():
    if "user_id" not in session:
        return jsonify({"answer": "Please login first."})

    question = request.json.get("question", "").lower()
    user_id = session["user_id"]

    queue = Queue.query.filter(
        Queue.user_id == user_id,
        Queue.status.in_(["Waiting", "Serving"])
    ).first()

    responses = []

    if any(word in question for word in ["hello", "hi", "hey"]):
        responses.append(f"Hello {session['user_name']}! 👋")

    if queue:
        if any(word in question for word in ["token", "number"]):
            responses.append(f"🎫 Token: {queue.token}")

        if any(word in question for word in ["department", "dept"]):
            responses.append(f"🏥 Department: {queue.department.name}")

        if any(word in question for word in ["status", "queue status"]):
            responses.append(f"📌 Status: {queue.status}")

        if "priority" in question:
            responses.append(f"⭐ Priority: {queue.priority} ({queue.priority_status})")

        if any(word in question for word in ["ahead", "before me"]):
            people_ahead = Queue.query.filter(
                Queue.department_id == queue.department_id,
                Queue.status == "Waiting",
                Queue.id < queue.id
            ).count()
            responses.append(f"👥 People Ahead: {people_ahead}")

        if any(word in question for word in ["wait", "waiting time", "how long", "eta", "estimate"]):
            avg_service = get_average_service_time(queue.department_id)
            people_ahead = Queue.query.filter(
                Queue.department_id == queue.department_id,
                Queue.status == "Waiting",
                Queue.id < queue.id
            ).count()
            estimated = round(avg_service * people_ahead, 1)
            responses.append(f"⏳ Estimated Wait: {estimated} minutes")

        if any(word in question for word in ["serving", "current", "current token", "who is serving"]):
            current = Queue.query.filter_by(
                department_id=queue.department_id,
                status="Serving"
            ).first()
            if current:
                responses.append(f"🟢 Currently Serving: {current.token}")
            else:
                responses.append("Nobody is being served right now.")

        if any(word in question for word in ["total waiting", "waiting people", "how many waiting", "waiting"]):
            total = Queue.query.filter_by(department_id=queue.department_id, status="Waiting").count()
            responses.append(f"👥 Total Waiting: {total}")
    else:
        responses.append("You don't have an active queue.")

    if any(word in question for word in ["shortest", "smallest queue", "least waiting", "fastest department"]):
        departments = Department.query.all()
        shortest = None
        minimum = float("inf")

        for dept in departments:
            waiting = Queue.query.filter_by(department_id=dept.id, status="Waiting").count()
            if waiting < minimum:
                minimum = waiting
                shortest = dept.name

        responses.append(f"🏢 Shortest Queue: {shortest} ({minimum} waiting)")

    if not responses:
        responses.append("Sorry, I don't understand that question.")

    return jsonify({"answer": "<br><br>".join(responses)})


@socketio.on("connect")
def handle_connect():
    print("Client Connected")


@socketio.on("register_user")
def register_user(data):
    user_id = str(data.get("user_id"))
    if user_id:
        connected_users[user_id] = request.sid
        print(f"User {user_id} connected -> {request.sid}")


@socketio.on("disconnect")
def handle_disconnect():
    for uid, sid in list(connected_users.items()):
        if sid == request.sid:
            del connected_users[uid]
            print(f"User {uid} disconnected")
            break


if __name__ == "__main__":
    # Local dev only. In production, gunicorn + the Procfile handles this.
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
