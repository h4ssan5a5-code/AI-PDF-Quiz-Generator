from flask import Flask, request, redirect, url_for, session, render_template, flash
import pdfplumber
import requests
import json
import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production-pls")

API_KEY = "add your grok api key here"
DB_PATH = "students.db"


# ─────────────────────────────────────────────
#  DATABASE SETUP
# ─────────────────────────────────────────────

def get_db():
    """Open a database connection (creates file if absent)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables on first run."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS students (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT    NOT NULL,
                email    TEXT    NOT NULL UNIQUE,
                password TEXT    NOT NULL,
                created  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                pdf_name   TEXT,
                total_score INTEGER,
                max_score   INTEGER,
                created    DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (student_id) REFERENCES students(id)
            );
        """)


init_db()


# ─────────────────────────────────────────────
#  AUTH DECORATOR
# ─────────────────────────────────────────────

def login_required(f):
    """Redirect to login page if user is not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "student_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pwd   = request.form.get("password", "")

        # Basic validation
        if not name or not email or not pwd:
            flash("All fields are required.", "error")
            return render_template("signup.html")

        if len(pwd) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("signup.html")

        hashed = generate_password_hash(pwd)

        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO students (name, email, password) VALUES (?, ?, ?)",
                    (name, email, hashed)
                )
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email already registered.", "error")
            return render_template("signup.html")

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pwd   = request.form.get("password", "")

        with get_db() as conn:
            student = conn.execute(
                "SELECT * FROM students WHERE email = ?", (email,)
            ).fetchone()

        if student and check_password_hash(student["password"], pwd):
            session["student_id"] = student["id"]
            session["student_name"] = student["name"]
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
#  STUDENT DASHBOARD
# ─────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    student_id = session["student_id"]

    with get_db() as conn:
        student = conn.execute(
            "SELECT * FROM students WHERE id = ?", (student_id,)
        ).fetchone()

        history = conn.execute(
            """SELECT * FROM quiz_sessions
               WHERE student_id = ?
               ORDER BY created DESC LIMIT 10""",
            (student_id,)
        ).fetchall()

    return render_template("dashboard.html", student=student, history=history)


# ─────────────────────────────────────────────
#  PDF TEXT EXTRACTION  (unchanged)
# ─────────────────────────────────────────────

def extract_text_from_pdf(pdf_file):
    text = ""
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text


# ─────────────────────────────────────────────
#  QUESTION GENERATION  (unchanged)
# ─────────────────────────────────────────────

def generate_questions(text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"""
You are an exam paper generator.

Generate 10 short questions WITH answers from the text.

Return ONLY JSON in this format:
[
  {{"question": "...", "answer": "..."}},
  ...
]

TEXT:
{text[:4000]}
"""
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}]
    }
    response = requests.post(url, headers=headers, json=data)
    result = response.json()
    try:
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print("ERROR:", result)
        return []


# ─────────────────────────────────────────────
#  ANSWER CHECKING  (unchanged)
# ─────────────────────────────────────────────

def check_answer(question, correct_answer, user_answer):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"""
You are a strict exam checker.

Question: {question}
Correct Answer: {correct_answer}
Student Answer: {user_answer}

Evaluate concept correctness.

Return ONLY JSON:
{{
  "score": (0-10),
  "feedback": "short explanation"
}}
"""
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}]
    }
    response = requests.post(url, headers=headers, json=data)
    result = response.json()
    try:
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)
    except:
        return {"score": 0, "feedback": "Error checking answer"}


# ─────────────────────────────────────────────
#  QUIZ ROUTES  (extended: now require login + store results)
# ─────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    if request.method == "POST":
        pdf = request.files.get("pdf")
        if not pdf:
            flash("Please upload a PDF.", "error")
            return render_template("home.html")

        text = extract_text_from_pdf(pdf)
        qa_pairs = generate_questions(text)

        if not qa_pairs:
            flash("Error generating questions. Try a different PDF.", "error")
            return render_template("home.html")

        # Store Q&A in session so /submit can access them
        session["qa_pairs"] = qa_pairs
        session["pdf_name"] = pdf.filename

        return render_template("quiz.html", qa_pairs=qa_pairs)

    return render_template("home.html")


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    qa_pairs = session.get("qa_pairs", [])
    pdf_name = session.get("pdf_name", "unknown.pdf")
    results  = []
    total_score = 0

    for i, qa in enumerate(qa_pairs):
        user_answer    = request.form.get(f"q{i}", "")
        question       = qa["question"]
        correct_answer = qa["answer"]

        evaluation = check_answer(question, correct_answer, user_answer)
        score      = evaluation.get("score", 0)
        feedback   = evaluation.get("feedback", "")
        total_score += score

        results.append({
            "question":       question,
            "user_answer":    user_answer,
            "correct_answer": correct_answer,
            "score":          score,
            "feedback":       feedback,
        })

    max_score = len(qa_pairs) * 10

    # Persist quiz session to DB, linked to the logged-in student
    with get_db() as conn:
        conn.execute(
            """INSERT INTO quiz_sessions (student_id, pdf_name, total_score, max_score)
               VALUES (?, ?, ?, ?)""",
            (session["student_id"], pdf_name, total_score, max_score)
        )

    # Clean up quiz data from session
    session.pop("qa_pairs", None)
    session.pop("pdf_name", None)

    return render_template(
        "results.html",
        results=results,
        total_score=total_score,
        max_score=max_score,
    )


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True)