from flask import Flask, redirect, url_for, session, request, jsonify
from authlib.integrations.flask_client import OAuth
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
import os
import json
import psycopg2
import psycopg2.extras
import smtplib
import random
from email.message import EmailMessage
from dotenv import load_dotenv
load_dotenv()

# ------------------- PostgreSQL -------------------
DB = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT")
}

def get_db():
    return psycopg2.connect(**DB)


app = Flask(__name__)

# ---- CORS: разрешаем только локальный фронтенд и включаем credentials ----
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN")
CORS(app, supports_credentials=True, resources={r"/*": {"origins": FRONTEND_ORIGIN}})
# ---- Сессии: в dev оставляем secure=False (на проде - True) ----
app.secret_key = os.getenv("SESSION_SECRET")
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'   # чтобы браузер принимал cookie между origin'ами
app.config['SESSION_COOKIE_SECURE'] = False      # в dev False (на prod нужно True + HTTPS)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=20)

# ------------------- Настройка Google OAuth -------------------
app.config['GOOGLE_CLIENT_ID'] = os.getenv("GOOGLE_CLIENT_ID")
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv("GOOGLE_CLIENT_SECRET")
app.config['GOOGLE_DISCOVERY_URL'] = "https://accounts.google.com/.well-known/openid-configuration"

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=app.config['GOOGLE_CLIENT_ID'],
    client_secret=app.config['GOOGLE_CLIENT_SECRET'],
    server_metadata_url=app.config['GOOGLE_DISCOVERY_URL'],
    client_kwargs={'scope': 'openid email profile'}
)

# ------------------- Создание таблиц
def init_pg():
    conn = get_db()
    cur = conn.cursor()

    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT,
            email TEXT UNIQUE NOT NULL,
            password TEXT,
            verified BOOLEAN DEFAULT FALSE,
            google_id TEXT
        );
    """)

    # Коды подтверждения email
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_codes (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    # Меню
    cur.execute("""
        CREATE TABLE IF NOT EXISTS menu (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            category TEXT NOT NULL
        );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS reservations (
        id SERIAL PRIMARY KEY,
        user_email TEXT NOT NULL,
        branch TEXT NOT NULL,
        date DATE NOT NULL,
        tables TEXT[] NOT NULL,
        guests INTEGER NOT NULL,
        notes TEXT,
        menu_items TEXT[],
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    #NEW
    cur.execute("""
        CREATE TABLE IF NOT EXISTS table_usage (
    id SERIAL PRIMARY KEY,
    table_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    date DATE NOT NULL,
    used_seats INTEGER NOT NULL
);
    """ )
    conn.commit()
    conn.close()

# ------------------- Email отправка
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

def send_email_code(to_email, code):
    """
    Отправляет код на почту. Если отправка упала — логируем и возвращаем False.
    Используем EmailMessage чтобы корректно задать Subject/From/To.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = "Код подтверждения регистрации"
        msg['From'] = EMAIL_SENDER
        msg['To'] = to_email
        msg.set_content(f"Ваш код подтверждения: {code}\n\nЭтот код действителен в ближайшее время.")

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print("send_email_code error:", e)
        return False

def generate_email_code(email):
    """
    Генерирует 6-значный код, сохраняет в email_codes и возвращает код.
    """
    code = str(random.randint(100000, 999999))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO email_codes (email, code) VALUES (%s, %s)", (email, code))
    conn.commit()
    conn.close()
    return code

# ------------------- Главная (для теста) -------------------
@app.route("/")
def index():
    user = session.get("user")
    if user:
        return f"""
        <h2>Главная страница</h2>
        <p>Вы вошли как: <b>{user.get('name')}</b> ({user.get('email')})</p>
        <p><a href="/menu">📋 Меню</a></p>
        <p><a href="/bookings">📅 Посмотреть брони</a></p>
        <p><a href="/logout">🚪 Выйти</a></p>
        """
    return """
    <h2>Главная страница</h2>
    <p>Тут работает сервер API. Для UI используйте frontend на localhost:3000</p>
    """

# ------------------- Регистрация (принимает JSON из фронтенда) -------------------
@app.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    name = data.get("name")
    email = data.get("email")
    password = data.get("password")

    if not name or not email or not password:
        return jsonify({"error": "Заполните все поля"}), 400

    hashed_pw = generate_password_hash(password)
    code = str(random.randint(100000, 999999))

    conn = get_db()
    cur = conn.cursor()

    # Проверяем, есть ли пользователь
    cur.execute("SELECT id FROM users WHERE email=%s", (email,))
    existing = cur.fetchone()

    if existing:
        conn.close()
        return jsonify({"error": "Пользователь уже существует"}), 409

    # Создаём, но verified = False
    cur.execute("""
        INSERT INTO users (name, email, password, verified)
        VALUES (%s, %s, %s, %s)
    """, (name, email, hashed_pw, False))

    # Код подтверждения
    cur.execute("""
        INSERT INTO email_codes (email, code)
        VALUES (%s, %s)
    """, (email, code))

    conn.commit()
    conn.close()

    # Отправляем email (если не отправилось — не падаем, но у фронтенда сообщаем)
    ok = send_email_code(email, code)
    if not ok:
        return jsonify({"message": "Пользователь создан. Но не удалось отправить код по почте."}), 201

    return jsonify({"message": "Пользователь создан. Подтвердите email."}), 201

# Новый endpoint: verify-email
@app.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    email = data.get("email")
    code = data.get("code")
    if not email or not code:
        return jsonify({"error": "email и code обязательны"}), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT code FROM email_codes WHERE email=%s ORDER BY id DESC LIMIT 1", (email,))
    record = cur.fetchone()

    if not record:
        conn.close()
        return jsonify({"error": "Код не найден"}), 400

    if record[0] != code:
        conn.close()
        return jsonify({"error": "Неверный код"}), 400

    # ставим пользователю verified = True
    cur.execute("UPDATE users SET verified=True WHERE email=%s", (email,))
    conn.commit()
    conn.close()

    return jsonify({"message": "Email подтвержден!"})

# endpoint для отправки только кода по email (используется в инструкции/тесте)
@app.post("/register/email")
def register_email():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    email = data.get("email")
    if not email:
        return jsonify({"error": "Email required"}), 400

    # Генерация кода (сохранение в БД)
    code = generate_email_code(email)

    ok = send_email_code(email, code)
    if not ok:
        return jsonify({"error": "Не удалось отправить email"}), 500

    return jsonify({"message": "Code sent"}), 200

# ------------------- Вход по email (принимает JSON) -------------------
@app.route("/login/email", methods=["POST"])
def login_email():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Введите email и пароль"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cur.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "Пользователь не найден"}), 404

    if not user["verified"]:
        return jsonify({"error": "Email не подтвержден"}), 403

    if not check_password_hash(user["password"], password):
        return jsonify({"error": "Неверный пароль"}), 401

    session["user"] = {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"]
    }

    return jsonify({"message": "Успешный вход", "user": session["user"]}), 200

# ------------------- Endpoint для проверки текущей сессии -------------------
@app.route("/auth/user", methods=["GET"])
def auth_user():
    user = session.get("user")
    if not user:
        return jsonify({"authenticated": False}), 200
    return jsonify({"authenticated": True, "user": user}), 200
# для профиля
@app.route("/user/bookings", methods=["GET"])
def user_bookings():
    user = session.get("user")
    if not user:
        return jsonify({"error": "Не авторизован"}), 401

    email = user["email"]

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM reservations WHERE user_email=%s ORDER BY date DESC", (email,))
    rows = cur.fetchall()
    conn.close()

    bookings = []
    for r in rows:
        bookings.append({
            "id": r["id"],
            "date": str(r["date"]),
            "branch": r["branch"],
            "persons": r["guests"],
            "menu": r["menu_items"],   # список меню
            "status": r["status"]
        })

    return jsonify({"bookings": bookings})

# ------------------- Вход через Google (redirect) -------------------
@app.route("/login/google")
def login_google():
    session.permanent = True
    redirect_uri = "http://localhost:5000/authorize"
    return google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    session.permanent = True
    token = google.authorize_access_token()
    user_info = google.get("https://openidconnect.googleapis.com/v1/userinfo").json()

    session["user"] = {
        "id": user_info.get("sub"),
        "name": user_info.get("name"),
        "email": user_info.get("email"),
    }

    return redirect(FRONTEND_ORIGIN)

# ------------------- Выход -------------------
@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("user", None)
    # если вызван AJAX — вернуть JSON
    if request.method == "POST" or request.is_json:
        return jsonify({"message": "Выход выполнен"}), 200
    return redirect(FRONTEND_ORIGIN)

# ------------------- Меню (оставил как есть) -------------------
@app.route("/menu", methods=["GET"])
def get_menu():
    with open("menu.json", "r", encoding="utf-8") as f:
        menu = json.load(f)
    return jsonify(menu)

# ------------------- Создание брони -------------------
@app.route("/book", methods=["POST"])
def create_booking():
    data = request.get_json()
    if os.path.exists("bookings.json"):
        with open("bookings.json", "r", encoding="utf-8") as f:
            bookings = json.load(f)
    else:
        bookings = []

    bookings.append(data)
    with open("bookings.json", "w", encoding="utf-8") as f:
        json.dump(bookings, f, ensure_ascii=False, indent=4)
    return jsonify({"message": "Бронь успешно добавлена"}), 201

# ------------------- Просмотр броней -------------------
@app.route("/bookings", methods=["GET"])
def view_bookings():
    if not os.path.exists("bookings.json"):
        return jsonify([])
    with open("bookings.json", "r", encoding="utf-8") as f:
        bookings = json.load(f)
    return jsonify(bookings)

# ------------------- Поиск брони -------------------
@app.route("/search_booking", methods=["GET"])
def search_booking():
    phone = request.args.get("phone")
    if not os.path.exists("bookings.json"):
        return jsonify({"message": "Файл с бронями не найден"}), 404
    with open("bookings.json", "r", encoding="utf-8") as f:
        bookings = json.load(f)
    results = [b for b in bookings if phone.replace("+", "") in b.get("phone", "").replace("+", "")]
    if not results:
        return jsonify({"message": "Бронь не найдена"}), 404
    return jsonify(results)
# NEW
@app.route("/occupied", methods=["GET"])
def get_occupied():
    branch = request.args.get("branch")
    date = request.args.get("date")

    if not branch or not date:
        return jsonify({"error": "branch и date обязательны"}), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT tables FROM reservations
        WHERE branch = %s AND date = %s AND status != 'cancelled'
    """, (branch, date))

    rows = cur.fetchall()
    conn.close()

    occupied = []
    for row in rows:
        occupied.extend(row[0])

    return jsonify({"occupied": occupied})
# Создание брони
@app.route("/reservation", methods=["POST"])
def create_reservation():
    data = request.get_json()

    required = ["user_email", "branch", "date", "tables", "guests"]
    if any(k not in data for k in required):
        return jsonify({"error": "Заполнены не все обязательные поля"}), 400

    user_email = data["user_email"]
    branch = data["branch"]
    date = data["date"]
    tables = data["tables"]       # ["L4-1", "C6-1"]
    guests = data["guests"]
    notes = data.get("notes", "")
    menu_items = data.get("menu_items", [])

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO reservations (user_email, branch, date, tables, guests, notes, menu_items)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (user_email, branch, date, tables, guests, notes, menu_items))

    res_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    return jsonify({"success": True, "reservation_id": res_id})

# ------------------- Pending booking (server-side temporary) -------------------
from flask import session as flask_session  # если не импортирован выше

@app.route("/pending", methods=["POST"])
def save_pending():
    """
    Сохраняет временную бронь в сессии (для незалогиненных).
    Ожидает JSON с payload, например:
    {
      "branch": "...",
      "date": "YYYY-MM-DD",
      "tables": ["L4-1"],
      "guests": 2,
      "notes": "...",
      "menu_items": ["Рамен 1", ...]
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Нет данных"}), 400

    # сохраняем в flask session
    flask_session["pending_booking"] = data
    # пометим время либо другую мета, если нужно
    return jsonify({"message": "pending saved"}), 200


@app.route("/pending/claim", methods=["POST"])
def claim_pending():
    """
    Если пользователь авторизован (session['user']), берет pending из session
    (или из тела, если прислали) и создаёт reservation в БД привязанную к user_email.
    Затем удаляет pending из session.
    """
    user = flask_session.get("user")
    if not user:
        return jsonify({"error": "Не авторизован"}), 401

    # Попытка взять pending из body (т.к. фронтенд может отправить localStorage copy)
    body = request.get_json(silent=True) or {}
    pending = body.get("pending") or flask_session.get("pending_booking")

    if not pending:
        return jsonify({"message": "Нет pending брони"}), 200

    # валидируем минимальные поля
    required = ["branch", "date", "tables", "guests"]
    if any(k not in pending for k in required):
        return jsonify({"error": "Заполнены не все обязательные поля в pending"}), 400

    user_email = user.get("email")
    branch = pending["branch"]
    date = pending["date"]
    tables = pending["tables"]
    guests = pending["guests"]
    notes = pending.get("notes", "")
    menu_items = pending.get("menu_items", [])

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reservations (user_email, branch, date, tables, guests, notes, menu_items)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_email, branch, date, tables, guests, notes, menu_items))
        res_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
    except Exception as e:
        print("claim_pending error:", e)
        return jsonify({"error": "Ошибка при создании брони"}), 500

    # Убираем pending из session
    flask_session.pop("pending_booking", None)

    return jsonify({"success": True, "reservation_id": res_id}), 200


# POST /reservation/confirm
@app.route("/reservation/confirm", methods=["POST"])
def confirm_reservation():
    data = request.get_json()
    res_id = data.get("reservation_id")
    if not res_id:
        return jsonify({"error": "reservation_id required"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE reservations SET status = 'confirmed' WHERE id = %s RETURNING id", (res_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if not row:
        return jsonify({"error": "Reservation not found"}), 404

    return jsonify({"success": True, "reservation_id": res_id}), 200

@app.route("/reservation/cancel", methods=["POST"])
def cancel_reservation():
    try:
        # 1. Сначала попробуем получить JSON
        data = request.get_json(silent=True) # Используем silent=True, чтобы не упасть, если JSON невалидный
        print("CANCEL RECEIVED (JSON):", data)

        # 2. Если JSON не получен, логируем сырые данные/заголовки
        if data is None:
            # Попробуем прочитать как текст, если get_json провалился
            raw_data = request.data.decode('utf-8')
            print("CANCEL FAILED. RAW DATA RECEIVED:", raw_data)
            print("HEADERS:", request.headers)
            return jsonify({"error": "No valid JSON payload received or 'id' is missing"}), 400

        # 3. Продолжаем, если JSON есть
        res_id = data.get("id")

        if not res_id:
            # Сюда мы, вероятно, попадаем. data - это {}, или 'id' - None/0
            return jsonify({"error": "Missing or invalid reservation id field in JSON"}), 400

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE reservations
            SET status = 'cancelled'
            WHERE id = %s
            RETURNING id;
        """, (res_id,))

        row = cur.fetchone()
        conn.commit()
        conn.close()

        if not row:
            return jsonify({"error": "Reservation not found"}), 404
        

        return jsonify({"success": True}), 200

    except Exception as e:
        print("cancel_reservation ERROR:", e)
        return jsonify({"error": str(e)}), 500


# Просмотр всех броней
@app.route("/bookings", methods=["GET"])
def get_bookings():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT * FROM reservations ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])

@app.route("/api/reserve-tables", methods=["POST"])
def reserve_tables():
    data = request.get_json()
    tables = data.get("tables")
    guests = data.get("guests")

    if not tables or not guests:
        return jsonify({"error": "tables и guests обязательны"}), 400

    return jsonify({
        "message": "Столы получены",
        "tables": tables,
        "guests": guests
    }), 200

# ------------------- Очистка -------------------
@app.route("/clear_bookings", methods=["DELETE"])
def clear_bookings():
    with open("bookings.json", "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=4)
    return jsonify({"message": "Все брони удалены"}), 200

@app.before_request
def log_request():
    print("REQUEST:", request.method, request.path)

@app.route("/reservation/confirm", methods=["OPTIONS"])
def confirm_reservation_options():
  return "", 200

# ------------------- Запуск -------------------
if __name__ == "__main__":
    init_pg()
    if not os.path.exists("bookings.json"):
        with open("bookings.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=4)

    # menu.json и menu_api инициализация s
    try:
        from menu_api import menu_api, init_menu
        init_menu()
        app.register_blueprint(menu_api)
    except Exception as e:
        print("menu_api not loaded:", e)

    print("Сервер запущен: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)

print(app.url_map)
