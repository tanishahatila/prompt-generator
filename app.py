from flask import (
    Flask, render_template, request, session,
    redirect, url_for, send_file, abort, flash
)
import uuid, io, os, requests
from werkzeug.security import generate_password_hash, check_password_hash
from requests_oauthlib import OAuth2Session
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from dotenv import load_dotenv
from supabase import create_client, Client, SupabaseException

# =============================
# Load ENV
# =============================
load_dotenv()

# =============================
# Flask App Setup
# =============================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "promptcraft-secret-key")

# =============================
# Supabase Setup (Safe)
# =============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except SupabaseException as e:
        print(f"Supabase initialization failed: {e}")
else:
    print("⚠️ Supabase ENV variables missing. Signup/Login will not work.")

# =============================
# Gemini API
# =============================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =============================
# Google OAuth Config
# =============================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# =============================
# Jinja Highlight Filter
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
# Project Intent Filter
# =============================
PROJECT_KEYWORDS = [
    "project", "app", "application", "website", "system",
    "platform", "tool", "software", "ai", "ml",
    "build", "create", "develop", "design"
]

def is_project_related(text):
    return any(word in text.lower() for word in PROJECT_KEYWORDS)

# =============================
# Auth Routes
# =============================
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not supabase:
        flash("Supabase not configured. Signup disabled.", "error")
        return render_template("signup.html")

    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return render_template("signup.html")

        # Check existing user
        res = supabase.table("users").select("*").eq("email", email).execute()
        if res.data:
            flash("Email already exists.", "error")
            return render_template("signup.html")

        # Insert new user
        res = supabase.table("users").insert({
            "id": str(uuid.uuid4()),
            "username": username,
            "email": email,
            "password": generate_password_hash(password)
        }).execute()

        user = res.data[0]
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        flash("Signup successful!", "success")
        return redirect(url_for("index"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not supabase:
        flash("Supabase not configured. Login disabled.", "error")
        return render_template("login.html")

    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        res = supabase.table("users").select("*").eq("email", email).execute()
        if not res.data:
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        user = res.data[0]
        if not user.get("password") or not check_password_hash(user["password"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# =============================
# Google OAuth Routes
# =============================
def get_google_cfg():
    return requests.get(GOOGLE_DISCOVERY_URL).json()


@app.route("/login/google")
def google_login():
    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=["openid", "email", "profile"]
    )
    google_cfg = get_google_cfg()
    auth_url, state = oauth.authorization_url(
        google_cfg["authorization_endpoint"], prompt="select_account"
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def google_callback():
    oauth_state = session.get("oauth_state")
    if not oauth_state:
        return redirect(url_for("login"))

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        state=oauth_state,
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
    username = userinfo.get("name") or email.split("@")[0]

    if supabase:
        res = supabase.table("users").select("*").eq("email", email).execute()
        if res.data:
            user = res.data[0]
        else:
            res = supabase.table("users").insert({
                "id": str(uuid.uuid4()),
                "username": username,
                "email": email,
                "password": None
            }).execute()
            user = res.data[0]
    else:
        flash("Supabase not configured. OAuth login disabled.", "error")
        return redirect(url_for("login"))

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))

# =============================
# AI Prompt Generator
# =============================
@app.route("/", methods=["GET", "POST"])
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if "chat" not in session:
        session["chat"] = [{
            "role": "assistant",
            "text": "Hi! 👋\nI am MICO Prompt Generator. Share your project idea."
        }]

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        if not query:
            return render_template("index.html", chat=session.get("chat"))

        session["chat"].append({"role": "user", "text": query})

        if not is_project_related(query):
            session["chat"].append({
                "role": "assistant",
                "text": "Please share a valid project idea (app, website, AI tool, software)."
            })
            return render_template("index.html", chat=session.get("chat"))

        prompt = f"Convert this project idea into a professional AI prompt:\n{query}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        headers = {"x-goog-api-key": GEMINI_API_KEY}

        try:
            res = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
                json=payload, headers=headers, timeout=20
            ).json()
            reply = res["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            reply = f"Error generating AI prompt: {e}"

        session["chat"].append({"role": "assistant", "text": reply})

    return render_template("index.html", chat=session.get("chat"))

# =============================
# Download TXT / PDF
# =============================
@app.route("/download/<int:idx>/txt")
def download_txt(idx):
    chat = session.get("chat", [])
    if idx < 0 or idx >= len(chat):
        abort(404)
    return send_file(io.BytesIO(chat[idx]["text"].encode()), as_attachment=True,
                     download_name="output.txt", mimetype="text/plain")


@app.route("/download/<int:idx>/pdf")
def download_pdf(idx):
    chat = session.get("chat", [])
    if idx < 0 or idx >= len(chat):
        abort(404)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    story = [Paragraph(chat[idx]["text"].replace("\n", "<br/>"), getSampleStyleSheet()["Normal"])]
    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True, download_name="output.pdf", mimetype="application/pdf")


# =============================
# Run App
# =============================
if __name__ == "__main__":
    app.run(debug=True)
