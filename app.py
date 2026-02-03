from flask import (
    Flask, render_template, request, session,
    redirect, url_for, send_file, abort, flash
)
import uuid, io, os, requests
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID
from werkzeug.security import generate_password_hash, check_password_hash
from requests_oauthlib import OAuth2Session
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

# =============================
# Load env
# =============================
load_dotenv()

# =============================
# Flask app
# =============================
app = Flask(__name__)
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY"
    )

# =============================
# Database (Supabase + SSL)
# =============================
DATABASE_URL = os.getenv("SUPABASE_DB_URL")
if not DATABASE_URL:
    raise RuntimeError("SUPABASE_DB_URL is not set")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =============================
# API Keys
# =============================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# =============================
# Model
# =============================
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=True)

# =============================
# Filters
# =============================
IMPORTANT_KEYWORDS = [
    "definition", "types", "example", "advantages",
    "disadvantages", "attack", "security",
    "steps", "process", "sql", "injection"
]

def highlight_keywords(text):
    if not text:
        return ""
    for word in IMPORTANT_KEYWORDS:
        text = text.replace(word, f"<span class='highlight'>{word}</span>")
        text = text.replace(word.capitalize(), f"<span class='highlight'>{word.capitalize()}</span>")
    return text

app.jinja_env.filters["highlight_keywords"] = highlight_keywords

PROJECT_KEYWORDS = [
    "project", "app", "application", "website",
    "system", "platform", "tool", "software",
    "ai", "ml", "build", "create", "develop", "design"
]

def is_project_related(text):
    return any(w in text.lower() for w in PROJECT_KEYWORDS)

# =============================
# Auth Routes
# =============================
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        password = request.form["password"].strip()

        if User.query.filter_by(email=email).first():
            flash("Email already exists", "error")
            return render_template("signup.html")

        user = User(username=username, email=email, password=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()

        session.clear()
        session["user_id"] = str(user.id)
        session["username"] = user.username

        return redirect(url_for("index"))

    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"].strip()

        user = User.query.filter_by(email=email).first()
        if user and user.password and check_password_hash(user.password, password):
            session.clear()
            session["user_id"] = str(user.id)
            session["username"] = user.username
            return redirect(url_for("index"))

        flash("Invalid credentials", "error")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# =============================
# Google OAuth
# =============================
def get_google_cfg():
    return requests.get(GOOGLE_DISCOVERY_URL).json()

@app.route("/login/google")
def google_login():
    google_cfg = get_google_cfg()
    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=["openid", "email", "profile"]
    )
    auth_url, state = oauth.authorization_url(google_cfg["authorization_endpoint"])
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/auth/callback")
def google_callback():
    if "oauth_state" not in session:
        return redirect(url_for("login"))

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        state=session["oauth_state"],
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    google_cfg = get_google_cfg()
    oauth.fetch_token(
        google_cfg["token_endpoint"],
        client_secret=GOOGLE_CLIENT_SECRET,
        authorization_response=request.url
    )

    userinfo = oauth.get(google_cfg["userinfo_endpoint"]).json()
    email = userinfo["email"]
    username = userinfo.get("name", email.split("@")[0])

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(username=username, email=email, password=None)
        db.session.add(user)
        db.session.commit()

    session.clear()
    session["user_id"] = str(user.id)
    session["username"] = user.username
    return redirect(url_for("index"))

# =============================
# AI Prompt Generator
# =============================
@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if "chat" not in session:
        session["chat"] = [{"role": "assistant", "text": "Hi 👋 Share your project idea."}]

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not is_project_related(query):
            session["chat"].append({"role": "assistant", "text": "Please share a valid project idea."})
        else:
            session["chat"].append({"role": "user", "text": query})

            payload = {"contents": [{"parts": [{"text": query}]}]}
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
                    headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
                    json=payload,
                    timeout=20
                ).json()
                reply = r["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                reply = f"AI Error: {e}"

            session["chat"].append({"role": "assistant", "text": reply})
        session.modified = True

    return render_template("index.html", chat=session["chat"])

# =============================
# Downloads
# =============================
@app.route("/download/<int:i>/txt")
def download_txt(i):
    msg = session["chat"][i]["text"]
    return send_file(io.BytesIO(msg.encode()), as_attachment=True, download_name="output.txt")

@app.route("/download/<int:i>/pdf")
def download_pdf(i):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf)
    doc.build([Paragraph(session["chat"][i]["text"], getSampleStyleSheet()["Normal"])])
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="output.pdf")

# =============================
# Error handler
# =============================
@app.errorhandler(Exception)
def handle_all_errors(error):
    return f"<h1>Internal Server Error</h1><pre>{error}</pre>", 500

# =============================
# Start
# =============================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    