from flask import (
    Flask, render_template, request, session,
    redirect, url_for, send_file, abort, flash
)
import uuid
from sqlalchemy.dialects.postgresql import UUID
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from requests_oauthlib import OAuth2Session
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
import requests, io, os
from dotenv import load_dotenv
# =============================
# ALLOW HTTP FOR OAUTH (DEV)
# =============================
load_dotenv()

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# =============================
# App setup
# =============================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "promptcraft-secret-key")

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("SUPABASE_DB_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =============================
# Google OAuth Config
# =============================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

# =============================
# Database Model
# =============================
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(100), nullable=False, unique=True)
    password = db.Column(db.String(200), nullable=True)


# =============================
# Jinja Filter
# =============================
IMPORTANT_KEYWORDS = [
    "definition", "types", "example", "advantages", "disadvantages",
    "attack", "security", "steps", "process", "sql", "injection"
]

def highlight_keywords(text):
    if not text:
        return ""
    for word in IMPORTANT_KEYWORDS:
        text = text.replace(word, f'<span class="highlight">{word}</span>')
        text = text.replace(word.capitalize(), f'<span class="highlight">{word.capitalize()}</span>')
    return text

app.jinja_env.filters["highlight_keywords"] = highlight_keywords

# =============================
# Project Intent Filter (NEW)
# =============================
PROJECT_KEYWORDS = [
    "project", "app", "application", "website", "system",
    "platform", "tool", "software", "ai", "ml",
    "build", "create", "develop", "design"
]

def is_project_related(text):
    text = text.lower()
    return any(word in text for word in PROJECT_KEYWORDS)

# =============================
# Auth Routes
# =============================
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already exists", "error")
            return render_template("signup.html")

        user = User(username=username, email=email, password=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()

        flash("Signup successful! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(email=email).first()
        if user and user.password and check_password_hash(user.password, password):
            session.clear()
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))

        flash("Invalid email or password", "error")

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
    if session.get("user_id"):
        return redirect(url_for("index"))

    google_cfg = get_google_cfg()

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=["openid", "email", "profile"]
    )

    authorization_url, state = oauth.authorization_url(
        google_cfg["authorization_endpoint"],
        prompt="select_account"
    )

    session.clear()
    session["oauth_state"] = state
    return redirect(authorization_url)


@app.route("/auth/callback")
def google_callback():
    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        state=session.get("oauth_state"),
        redirect_uri=GOOGLE_REDIRECT_URI
    )

    google_cfg = get_google_cfg()

    oauth.fetch_token(
        google_cfg["token_endpoint"],
        client_secret=GOOGLE_CLIENT_SECRET,
        authorization_response=request.url
    )

    userinfo = oauth.get(google_cfg["userinfo_endpoint"]).json()

    if not userinfo.get("email_verified"):
        flash("Google email not verified", "error")
        return redirect(url_for("login"))

    email = userinfo["email"]
    username = userinfo.get("name") or email.split("@")[0]

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(username=username, email=email, password=None)
        db.session.add(user)
        db.session.commit()

    session.clear()
    session["user_id"] = user.id
    session["username"] = user.username
    return redirect(url_for("index"))

# =============================
# AI Prompt Generator
# =============================
@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        session["chat"] = [{
            "role": "assistant",
            "text": (
                "Hi! 👋\n\n"
                "I am MICO Prompt Generator.\n"
                "Give me your project idea and I will generate:\n"
                "- A professional AI prompt\n"
                "- Clear project requirements"
            )
        }]

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if query:
            session["chat"].append({"role": "user", "text": query})

            # ❌ Non-project query handling (NEW)
            if not is_project_related(query):
                reply = (
                    "Thank you for your message.\n\n"
                    "I am a dedicated AI Prompt Generator and can only assist "
                    "with converting project ideas into professional AI prompts "
                    "and structured requirements.\n\n"
                    "Please share a valid project idea such as an app, website, "
                    "AI tool, or software system."
                )
                session["chat"].append({"role": "assistant", "text": reply})
                session.modified = True
                return render_template("index.html", chat=session.get("chat", []))

            prompt = f"""
You are a professional AI prompt engineer.

Convert the following project idea into:
1. A clear AI-understandable prompt
2. A structured list of project requirements

Project Idea:
{query}

Output Format:
Prompt:
Requirements:
- Requirement 1
- Requirement 2
"""

            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

            try:
                res = requests.post(url, json=payload, headers=headers, timeout=20).json()
                if "candidates" in res and res["candidates"]:
                    reply = res["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    # Check if it's a quota error
                    error = res.get('error', {})
                    if isinstance(error, dict) and error.get('status') == 'RESOURCE_EXHAUSTED':
                        reply = "⚠️ Daily API quota reached. Please try again tomorrow or upgrade your plan."
                    else:
                        reply = f"Error generating response: {res}"
            except Exception as e:
                reply = f"Error generating response: {e}"

            session["chat"].append({"role": "assistant", "text": reply})
            session.modified = True

    return render_template("index.html", chat=session.get("chat", []))


# =============================
# DOWNLOAD TXT (BY INDEX)
# =============================
@app.route("/download/<int:idx>/txt")
def download_txt_by_index(idx):
    chat = session.get("chat")
    if not chat or idx < 0 or idx >= len(chat):
        abort(404)

    msg = chat[idx]["text"]

    return send_file(
        io.BytesIO(msg.encode("utf-8")),
        as_attachment=True,
        download_name="prompt_output.txt",
        mimetype="text/plain"
    )


# =============================
# DOWNLOAD PDF (BY INDEX)
# =============================
@app.route("/download/<int:idx>/pdf")
def download_pdf_by_index(idx):
    chat = session.get("chat")
    if not chat or idx < 0 or idx >= len(chat):
        abort(404)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    styles = getSampleStyleSheet()
    story = []

    text = chat[idx]["text"]
    story.append(
        Paragraph(text.replace("\n", "<br/>"), styles["Normal"])
    )

    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="prompt_output.pdf",
        mimetype="application/pdf"
    )


# =============================
# Run
# =============================
# if __name__ == "__main__":
#     with app.app_context():
#         db.create_all()
#     app.run(debug=True)
