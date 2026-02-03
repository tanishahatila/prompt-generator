from flask import (
    Flask, render_template, request, redirect,
    url_for, send_file, flash
)
import uuid, io, os, requests
from werkzeug.security import generate_password_hash, check_password_hash
from requests_oauthlib import OAuth2Session
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from dotenv import load_dotenv
from supabase import create_client

# =============================
# ENV
# =============================
load_dotenv()

# =============================
# App setup
# =============================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "promptcraft-secret-key")

# =============================
# Supabase Client
# =============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")  # use anon key for serverless
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase ENV variables missing")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =============================
# Gemini API
# =============================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =============================
# Google OAuth
# =============================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

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
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password_raw = request.form.get("password", "").strip()

        if not username or not email or not password_raw:
            flash("All fields are required", "error")
            return render_template("signup.html")

        password = generate_password_hash(password_raw)
        existing = supabase.table("users").select("*").eq("email", email).execute()
        if existing.data:
            flash("Email already exists", "error")
            return render_template("signup.html")

        res = supabase.table("users").insert({
            "id": str(uuid.uuid4()),
            "username": username,
            "email": email,
            "password": password
        }).execute()

        user = res.data[0]
        flash("Signup successful!", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        res = supabase.table("users").select("*").eq("email", email).execute()
        if not res.data:
            flash("Invalid email or password", "error")
            return render_template("login.html")

        user = res.data[0]
        if not user["password"] or not check_password_hash(user["password"], password):
            flash("Invalid email or password", "error")
            return render_template("login.html")

        # Store user_id in a cookie-safe session for serverless
        response = redirect(url_for("index"))
        response.set_cookie("user_id", user["id"])
        response.set_cookie("username", user["username"])
        return response

    return render_template("login.html")


@app.route("/logout")
def logout():
    response = redirect(url_for("login"))
    response.delete_cookie("user_id")
    response.delete_cookie("username")
    return response

# =============================
# Google OAuth
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
        google_cfg["authorization_endpoint"],
        prompt="select_account"
    )
    response = redirect(auth_url)
    response.set_cookie("oauth_state", state)
    return response


@app.route("/auth/callback")
def google_callback():
    state = request.cookies.get("oauth_state")
    if not state:
        return redirect(url_for("login"))

    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        state=state,
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

    res = supabase.table("users").select("*").eq("email", email).execute()
    if res.data:
        user = res.data[0]
    else:
        insert = supabase.table("users").insert({
            "id": str(uuid.uuid4()),
            "username": username,
            "email": email,
            "password": None
        }).execute()
        user = insert.data[0]

    response = redirect(url_for("index"))
    response.set_cookie("user_id", user["id"])
    response.set_cookie("username", user["username"])
    return response

# =============================
# AI Prompt Generator
# =============================
@app.route("/", methods=["GET", "POST"])
def index():
    user_id = request.cookies.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    chat_res = supabase.table("chat_history").select("*").eq("user_id", user_id).execute()
    chat = chat_res.data if chat_res.data else [{
        "role": "assistant",
        "text": "Hi! 👋\n\nI am MICO Prompt Generator.\nShare your project idea."
    }]

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        chat.append({"role": "user", "text": query})

        if not is_project_related(query):
            chat.append({"role": "assistant", "text": "Please share a valid project idea."})
        else:
            try:
                prompt = f"Convert this project idea into a professional AI prompt:\n{query}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                headers = {
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY
                }
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
                    json=payload,
                    headers=headers,
                    timeout=10
                )
                r.raise_for_status()
                data = r.json()
                reply = data["candidates"][0]["content"]["parts"][0]["text"]
                chat.append({"role": "assistant", "text": reply})
            except Exception as e:
                chat.append({"role": "assistant", "text": f"Error generating AI prompt: {e}"})

        # Save chat to Supabase
        supabase.table("chat_history").insert([{
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "role": msg["role"],
            "text": msg["text"]
        } for msg in chat]).execute()

    return render_template("index.html", chat=chat)

# =============================
# Download TXT / PDF
# =============================
@app.route("/download/<int:idx>/txt")
def download_txt(idx):
    user_id = request.cookies.get("user_id")
    chat_res = supabase.table("chat_history").select("*").eq("user_id", user_id).execute()
    chat = chat_res.data
    msg = chat[idx]["text"]
    return send_file(io.BytesIO(msg.encode()), as_attachment=True, download_name="output.txt", mimetype="text/plain")


@app.route("/download/<int:idx>/pdf")
def download_pdf(idx):
    user_id = request.cookies.get("user_id")
    chat_res = supabase.table("chat_history").select("*").eq("user_id", user_id).execute()
    chat = chat_res.data
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    story = [Paragraph(chat[idx]["text"].replace("\n", "<br/>"), getSampleStyleSheet()["Normal"])]
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="output.pdf", mimetype="application/pdf")


if __name__ == "__main__":
    app.run(debug=True)
