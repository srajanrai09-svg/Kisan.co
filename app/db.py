"""SQLite storage: users, sessions, products, orders, pincodes and the chat log. Works fully offline."""
import sqlite3
import time
from contextlib import contextmanager

from . import config
from .textutil import stem

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  role       TEXT NOT NULL CHECK (role IN ('farmer','customer')),
  name       TEXT NOT NULL,
  phone      TEXT NOT NULL UNIQUE,
  pw_hash    TEXT NOT NULL,
  farm_name  TEXT,
  area       TEXT NOT NULL DEFAULT '',
  address    TEXT NOT NULL DEFAULT '',
  pincode    TEXT NOT NULL DEFAULT '',
  lat        REAL,
  lng        REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  farmer_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  category    TEXT NOT NULL,
  price       REAL NOT NULL,
  unit        TEXT NOT NULL,
  qty         REAL NOT NULL DEFAULT 0,
  organic     INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  image       TEXT NOT NULL DEFAULT '',
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_farmer ON products(farmer_id);
CREATE TABLE IF NOT EXISTS orders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id  INTEGER NOT NULL REFERENCES users(id),
  farmer_id    INTEGER NOT NULL REFERENCES users(id),
  status       TEXT NOT NULL,
  total        REAL NOT NULL,
  address      TEXT NOT NULL,
  pincode      TEXT NOT NULL DEFAULT '',
  note         TEXT NOT NULL DEFAULT '',
  otp_nonce    TEXT,
  otp_attempts INTEGER NOT NULL DEFAULT 0,
  cancelled_by TEXT,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_farmer ON orders(farmer_id);
CREATE TABLE IF NOT EXISTS order_items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id INTEGER,
  name       TEXT NOT NULL,
  unit       TEXT NOT NULL,
  price      REAL NOT NULL,
  qty        REAL NOT NULL,
  line_total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pincodes (
  pincode TEXT PRIMARY KEY,
  lat     REAL NOT NULL,
  lng     REAL NOT NULL,
  n       INTEGER NOT NULL DEFAULT 1,
  source  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
  name       TEXT PRIMARY KEY,
  emoji      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admin_auth (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  pw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ip     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chat_log (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  question TEXT NOT NULL,
  answer   TEXT NOT NULL,
  mode     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  customer_id INTEGER NOT NULL REFERENCES users(id),
  farmer_id INTEGER NOT NULL REFERENCES users(id),
  rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment TEXT NOT NULL DEFAULT '',
  admin_reply TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  replied_at TEXT
);
CREATE TABLE IF NOT EXISTS farmer_complaints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  farmer_id INTEGER NOT NULL REFERENCES users(id),
  subject TEXT NOT NULL,
  message TEXT NOT NULL,
  admin_reply TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  replied_at TEXT
);
"""

USER_COLS = "id, role, name, phone, farm_name, area, address, pincode, lat, lng, created_at, blocked"


class OrderError(ValueError):
    pass


class StockError(OrderError):
    pass


@contextmanager
def conn():
    c = sqlite3.connect(config.SQLITE_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        try:
            c.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        c.executescript(SCHEMA)
        # Databases made before the admin panel existed get the new column here.
        if "blocked" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        # First run only: the starting category list. After that the admin panel owns it.
        if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            c.executemany("INSERT INTO categories(name, emoji) VALUES (?,?)", config.DEFAULT_CATEGORIES)


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------ users and sessions
def create_user(role, name, phone, pw_hash, farm_name, area, address, pincode, lat, lng):
    with conn() as c:
        return c.execute(
            """INSERT INTO users(role, name, phone, pw_hash, farm_name, area, address, pincode, lat, lng)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (role, name, phone, pw_hash, farm_name, area, address, pincode, lat, lng),
        ).lastrowid


def user_by_phone(phone):
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return dict(r) if r else None


def user_by_id(uid):
    with conn() as c:
        r = c.execute(f"SELECT {USER_COLS} FROM users WHERE id = ?", (uid,)).fetchone()
        return dict(r) if r else None


def update_user(uid, **fields):
    allowed = {"name", "farm_name", "area", "address", "pincode", "lat", "lng"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE id = ?", (*fields.values(), uid))


def create_session(uid, token_hash, ttl_days):
    with conn() as c:
        c.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
                  (token_hash, uid, time.time() + ttl_days * 86400))


def user_by_token_hash(token_hash):
    with conn() as c:
        r = c.execute(
            f"""SELECT {', '.join('u.' + x.strip() for x in USER_COLS.split(','))}
                FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?""",
            (token_hash, time.time()),
        ).fetchone()
        return dict(r) if r else None


def delete_session(token_hash):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def purge_sessions():
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


# ------------------------------------------------------------------ farms
def farms():
    """Farmers who have a location. id is the farmer's user id."""
    with conn() as c:
        return _rows(c.execute(
            """SELECT id, COALESCE(NULLIF(farm_name, ''), name) AS name, area, pincode, lat, lng
               FROM users WHERE role = 'farmer' AND lat IS NOT NULL AND lng IS NOT NULL ORDER BY id"""))


def product_counts():
    with conn() as c:
        return {r["farmer_id"]: r["n"] for r in c.execute(
            "SELECT farmer_id, COUNT(*) AS n FROM products WHERE active = 1 AND qty > 0 GROUP BY farmer_id")}


# ------------------------------------------------------------------ products
_SELECT_PRODUCT = """
SELECT p.id, p.farmer_id, p.name, p.category, p.price, p.unit, p.qty, p.organic, p.description, p.image,
       p.active, p.created_at, p.updated_at,
       (SELECT ROUND(AVG(f.rating),1) FROM purchase_feedback f JOIN order_items oi ON oi.order_id=f.order_id WHERE oi.product_id=p.id) AS rating,
       (SELECT COUNT(DISTINCT f.id) FROM purchase_feedback f JOIN order_items oi ON oi.order_id=f.order_id WHERE oi.product_id=p.id) AS rating_count,
       p.farmer_id AS farm_id, COALESCE(NULLIF(u.farm_name, ''), u.name) AS farm,
       u.area, u.pincode, u.lat, u.lng
FROM products p JOIN users u ON u.id = p.farmer_id
"""


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def products(farmer_id=None, q=None, category=None, available_only=True):
    """available_only=True: active and in stock (what customers see). False: every active listing."""
    where, args = [], []
    if available_only:
        where.append("p.active = 1 AND p.qty > 0")
    if farmer_id:
        where.append("p.farmer_id = ?")
        args.append(farmer_id)
    if category:
        where.append("p.category = ?")
        args.append(category)
    for word in (q or "").lower().split()[:5]:
        forms = {word, stem(word)}
        clause = []
        for f in forms:
            for col in ("p.name", "p.category", "u.farm_name", "u.name", "u.area", "u.pincode"):
                clause.append(f"{col} LIKE ? ESCAPE '\\'")
                args.append(_like(f))
        where.append("(" + " OR ".join(clause) + ")")
    sql = _SELECT_PRODUCT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.id DESC"
    with conn() as c:
        return [dict(r, organic=bool(r["organic"]), active=bool(r["active"])) for r in c.execute(sql, args)]


def my_products(farmer_id):
    """A farmer's own listings, including hidden and sold-out ones."""
    with conn() as c:
        return [dict(r, organic=bool(r["organic"]), active=bool(r["active"]))
                for r in c.execute(_SELECT_PRODUCT + " WHERE p.farmer_id = ? ORDER BY p.id DESC", (farmer_id,))]


def get_product(pid):
    with conn() as c:
        r = c.execute(_SELECT_PRODUCT + " WHERE p.id = ?", (pid,)).fetchone()
        return dict(r, organic=bool(r["organic"]), active=bool(r["active"])) if r else None


def add_product(farmer_id, name, category, price, unit, qty, organic, description, image):
    with conn() as c:
        return c.execute(
            """INSERT INTO products(farmer_id, name, category, price, unit, qty, organic, description, image)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (farmer_id, name, category, price, unit, qty, int(organic), description, image),
        ).lastrowid


def update_product(pid, farmer_id, **fields):
    allowed = {"name", "category", "price", "unit", "qty", "organic", "description", "image", "active"}
    fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
    if not fields:
        return True
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = CURRENT_TIMESTAMP"
    with conn() as c:
        return c.execute(f"UPDATE products SET {sets} WHERE id = ? AND farmer_id = ?",
                         (*fields.values(), pid, farmer_id)).rowcount == 1


def delete_product(pid, farmer_id):
    """Returns the image path of the deleted product, or None if it was not found."""
    with conn() as c:
        r = c.execute("SELECT image FROM products WHERE id = ? AND farmer_id = ?", (pid, farmer_id)).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM products WHERE id = ?", (pid,))
        return r["image"] or ""


# ------------------------------------------------------------------ orders
def create_orders(customer_id, items, address, pincode, note):
    """items: list of (product_id, qty). Places one order per farmer in a single transaction.
    Stock is taken off the shelf at the same time, so two customers cannot buy the last kilo."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        by_farmer = {}
        for pid, qty in items:
            qty = round(qty, 3)
            p = c.execute("SELECT id, farmer_id, name, unit, price, qty, active FROM products WHERE id = ?",
                          (pid,)).fetchone()
            if not p or not p["active"]:
                raise OrderError("One of the products is no longer available.")
            cur = c.execute(
                "UPDATE products SET qty = ROUND(qty - ?, 3), updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND active = 1 AND ROUND(qty, 3) >= ?", (qty, pid, qty))
            if cur.rowcount != 1:
                raise StockError(f"Only {p['qty']:g} {p['unit']} of {p['name']} left.")
            by_farmer.setdefault(p["farmer_id"], []).append((p, qty))
        ids = []
        for fid, lines in by_farmer.items():
            total = round(sum(round(p["price"] * q, 2) for p, q in lines), 2)
            oid = c.execute(
                """INSERT INTO orders(customer_id, farmer_id, status, total, address, pincode, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (customer_id, fid, "placed", total, address, pincode, note)).lastrowid
            for p, q in lines:
                c.execute(
                    """INSERT INTO order_items(order_id, product_id, name, unit, price, qty, line_total)
                       VALUES (?,?,?,?,?,?,?)""",
                    (oid, p["id"], p["name"], p["unit"], p["price"], q, round(p["price"] * q, 2)))
            ids.append(oid)
        return ids


_SELECT_ORDER = """
SELECT o.*, cu.name AS customer_name, cu.phone AS customer_phone,
       fa.name AS farmer_name, fa.farm_name, fa.phone AS farmer_phone, fa.area AS farm_area
FROM orders o JOIN users cu ON cu.id = o.customer_id JOIN users fa ON fa.id = o.farmer_id
"""


def _attach_items(c, orders):
    if not orders:
        return orders
    ids = [o["id"] for o in orders]
    marks = ",".join("?" * len(ids))
    by = {}
    for r in c.execute(f"SELECT order_id, name, unit, price, qty, line_total FROM order_items "
                       f"WHERE order_id IN ({marks}) ORDER BY id", ids):
        by.setdefault(r["order_id"], []).append({k: r[k] for k in ("name", "unit", "price", "qty", "line_total")})
    for o in orders:
        o["items"] = by.get(o["id"], [])
    return orders


def get_order(oid):
    with conn() as c:
        r = c.execute(_SELECT_ORDER + " WHERE o.id = ?", (oid,)).fetchone()
        return _attach_items(c, [dict(r)])[0] if r else None


def list_orders(role, uid):
    col = "farmer_id" if role == "farmer" else "customer_id"
    with conn() as c:
        rows = [dict(r) for r in c.execute(_SELECT_ORDER + f" WHERE o.{col} = ? ORDER BY o.id DESC LIMIT 200", (uid,))]
        return _attach_items(c, rows)


def save_purchase_feedback(order_id, customer_id, rating, comment):
    with conn() as c:
        order = c.execute("SELECT farmer_id,status FROM orders WHERE id=? AND customer_id=?", (order_id, customer_id)).fetchone()
        if not order or order["status"] != "delivered":
            return "ineligible"
        try:
            c.execute("INSERT INTO purchase_feedback(order_id,customer_id,farmer_id,rating,comment) VALUES(?,?,?,?,?)",
                      (order_id, customer_id, order["farmer_id"], rating, comment))
        except sqlite3.IntegrityError:
            return "exists"
        return "ok"


def customer_feedback(customer_id):
    with conn() as c:
        return _rows(c.execute("SELECT f.*,o.id AS order_id FROM purchase_feedback f JOIN orders o ON o.id=f.order_id WHERE f.customer_id=? ORDER BY f.id DESC", (customer_id,)))


def farmer_feedback(farmer_id):
    with conn() as c:
        return _rows(c.execute("SELECT f.*,u.name AS customer_name FROM purchase_feedback f JOIN users u ON u.id=f.customer_id WHERE f.farmer_id=? ORDER BY f.id DESC", (farmer_id,)))


def submit_complaint(farmer_id, subject, message):
    with conn() as c:
        return c.execute("INSERT INTO farmer_complaints(farmer_id,subject,message) VALUES(?,?,?)", (farmer_id, subject, message)).lastrowid


def farmer_complaints(farmer_id):
    with conn() as c:
        return _rows(c.execute("SELECT * FROM farmer_complaints WHERE farmer_id=? ORDER BY id DESC", (farmer_id,)))


def admin_feedback():
    with conn() as c:
        return _rows(c.execute("SELECT f.*,cu.name AS customer_name,fa.name AS farmer_name,fa.farm_name FROM purchase_feedback f JOIN users cu ON cu.id=f.customer_id JOIN users fa ON fa.id=f.farmer_id ORDER BY f.id DESC"))


def admin_complaints():
    with conn() as c:
        return _rows(c.execute("SELECT c.*,u.name AS farmer_name,u.farm_name FROM farmer_complaints c JOIN users u ON u.id=c.farmer_id ORDER BY c.id DESC"))


def reply_feedback(feedback_id, reply):
    with conn() as c:
        return c.execute("UPDATE purchase_feedback SET admin_reply=?,replied_at=CURRENT_TIMESTAMP WHERE id=?", (reply, feedback_id)).rowcount == 1


def reply_complaint(complaint_id, reply):
    with conn() as c:
        return c.execute("UPDATE farmer_complaints SET admin_reply=?,replied_at=CURRENT_TIMESTAMP WHERE id=?", (reply, complaint_id)).rowcount == 1


def delivered_units_sold():
    with conn() as c:
        row = c.execute("SELECT COALESCE(SUM(i.qty),0) AS units,COUNT(DISTINCT o.id) AS orders FROM orders o JOIN order_items i ON i.order_id=o.id WHERE o.status='delivered'").fetchone()
        return {"units": row["units"], "orders": row["orders"]}


def transition(oid, who_col, who_id, allowed_from, to, **extra):
    """Atomically move an order to a new status. Returns True only if it was in an allowed state."""
    assert who_col in ("farmer_id", "customer_id")
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"] + [f"{k} = ?" for k in extra]
    marks = ",".join("?" * len(allowed_from))
    with conn() as c:
        return c.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = ? AND {who_col} = ? AND status IN ({marks})",
            (to, *extra.values(), oid, who_id, *allowed_from)).rowcount == 1


def cancel_order(oid, who_col, who_id, allowed_from, cancelled_by):
    """Cancel and put the stock back, in one transaction."""
    assert who_col in ("farmer_id", "customer_id")
    marks = ",".join("?" * len(allowed_from))
    with conn() as c:
        cur = c.execute(
            f"UPDATE orders SET status = 'cancelled', cancelled_by = ?, otp_nonce = NULL, "
            f"updated_at = CURRENT_TIMESTAMP WHERE id = ? AND {who_col} = ? AND status IN ({marks})",
            (cancelled_by, oid, who_id, *allowed_from))
        if cur.rowcount != 1:
            return False
        items = c.execute("SELECT product_id, qty FROM order_items WHERE order_id = ?", (oid,)).fetchall()
        for it in items:
            if it["product_id"] is not None:
                c.execute("UPDATE products SET qty = ROUND(qty + ?, 3), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                          (it["qty"], it["product_id"]))
        return True


def use_otp_attempt(oid, farmer_id):
    """Counts one OTP try. False means the order is not out for delivery or the tries are used up."""
    with conn() as c:
        return c.execute(
            "UPDATE orders SET otp_attempts = otp_attempts + 1 "
            "WHERE id = ? AND farmer_id = ? AND status = 'out_for_delivery' AND otp_attempts < ?",
            (oid, farmer_id, config.OTP_MAX_ATTEMPTS)).rowcount == 1


# ------------------------------------------------------------------ pincodes
def get_pincode(pin):
    with conn() as c:
        r = c.execute("SELECT * FROM pincodes WHERE pincode = ?", (pin,)).fetchone()
        return dict(r) if r else None


def set_pincode(pin, lat, lng, source):
    with conn() as c:
        c.execute(
            "INSERT INTO pincodes(pincode, lat, lng, n, source) VALUES (?,?,?,1,?) "
            "ON CONFLICT(pincode) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, n = 1, source = excluded.source",
            (pin, lat, lng, source))


def learn_pincode(pin, lat, lng):
    """A farm registered here: use the average of the farms in a pincode as its centre."""
    row = get_pincode(pin)
    if row and row["source"] == "farm":
        n = row["n"] + 1
        lat, lng = (row["lat"] * row["n"] + lat) / n, (row["lng"] * row["n"] + lng) / n
        with conn() as c:
            c.execute("UPDATE pincodes SET lat = ?, lng = ?, n = ? WHERE pincode = ?", (lat, lng, n, pin))
    else:
        set_pincode(pin, lat, lng, "farm")


# ------------------------------------------------------------------ chat log
def log_chat(question, answer, mode):
    with conn() as c:
        c.execute("INSERT INTO chat_log(question, answer, mode) VALUES (?,?,?)", (question, answer, mode))


# ------------------------------------------------------------------ categories (managed from the admin panel)
def category_names():
    with conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM categories ORDER BY rowid")]


def category_emojis():
    with conn() as c:
        return {r["name"]: r["emoji"] for r in c.execute("SELECT name, emoji FROM categories") if r["emoji"]}


def admin_categories():
    with conn() as c:
        return _rows(c.execute(
            "SELECT c.name, c.emoji, (SELECT COUNT(*) FROM products p WHERE p.category = c.name) AS products "
            "FROM categories c ORDER BY c.rowid"))


def add_category(name, emoji):
    with conn() as c:
        c.execute("INSERT INTO categories(name, emoji) VALUES (?,?)", (name, emoji))


def set_category_emoji(name, emoji):
    with conn() as c:
        return c.execute("UPDATE categories SET emoji = ? WHERE name = ?", (emoji, name)).rowcount == 1


def delete_category(name):
    """Returns 'ok', 'missing', 'in_use' or 'last'."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone():
            return "missing"
        if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] <= 1:
            return "last"
        if c.execute("SELECT 1 FROM products WHERE category = ? LIMIT 1", (name,)).fetchone():
            return "in_use"
        c.execute("DELETE FROM categories WHERE name = ?", (name,))
        return "ok"


# ------------------------------------------------------------------ admin: login, sessions, audit log
def admin_pw_hash():
    with conn() as c:
        r = c.execute("SELECT pw_hash FROM admin_auth WHERE id = 1").fetchone()
        return r["pw_hash"] if r else None


def set_admin_pw_hash(pw_hash):
    """Saves the admin password and logs every admin out."""
    with conn() as c:
        c.execute("INSERT INTO admin_auth(id, pw_hash) VALUES (1, ?) "
                  "ON CONFLICT(id) DO UPDATE SET pw_hash = excluded.pw_hash", (pw_hash,))
        c.execute("DELETE FROM admin_sessions")


def create_admin_session(token_hash, ttl_seconds):
    with conn() as c:
        c.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (time.time(),))
        c.execute("INSERT INTO admin_sessions(token_hash, expires_at) VALUES (?,?)",
                  (token_hash, time.time() + ttl_seconds))


def admin_session_ok(token_hash):
    with conn() as c:
        return c.execute("SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
                         (token_hash, time.time())).fetchone() is not None


def delete_admin_session(token_hash):
    with conn() as c:
        c.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))


def log_admin(action, detail="", ip=""):
    with conn() as c:
        c.execute("INSERT INTO admin_log(action, detail, ip) VALUES (?,?,?)", (action, str(detail)[:300], ip))


def admin_log_rows(limit=100):
    with conn() as c:
        return _rows(c.execute("SELECT ts, action, detail, ip FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)))


# ------------------------------------------------------------------ admin: users, products, orders, images
def admin_stats():
    with conn() as c:
        one = lambda sql: c.execute(sql).fetchone()[0]
        return {
            "farmers": one("SELECT COUNT(*) FROM users WHERE role = 'farmer'"),
            "customers": one("SELECT COUNT(*) FROM users WHERE role = 'customer'"),
            "blocked": one("SELECT COUNT(*) FROM users WHERE blocked = 1"),
            "products": one("SELECT COUNT(*) FROM products"),
            "orders": one("SELECT COUNT(*) FROM orders"),
            "open_orders": one("SELECT COUNT(*) FROM orders WHERE status IN ('placed','accepted','out_for_delivery')"),
            "delivered": one("SELECT COUNT(*) FROM orders WHERE status = 'delivered'"),
            "sales": round(one("SELECT COALESCE(SUM(total), 0) FROM orders WHERE status = 'delivered'"), 2),
            "chats": one("SELECT COUNT(*) FROM chat_log"),
        }


def admin_users():
    with conn() as c:
        return _rows(c.execute(
            "SELECT id, role, name, phone, farm_name, area, pincode, created_at, blocked "
            "FROM users ORDER BY id DESC LIMIT 1000"))


def set_blocked(uid, value):
    """Block or unblock. Blocking ends their sessions and hides a farmer's listings."""
    with conn() as c:
        r = c.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if not r:
            return False
        c.execute("UPDATE users SET blocked = ? WHERE id = ?", (int(value), uid))
        if value:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            if r["role"] == "farmer":
                c.execute("UPDATE products SET active = 0 WHERE farmer_id = ?", (uid,))
        return True


def admin_products():
    with conn() as c:
        return _rows(c.execute(
            "SELECT p.id, p.name, p.category, p.price, p.unit, p.qty, p.active, p.image, p.created_at, "
            "COALESCE(NULLIF(u.farm_name, ''), u.name) AS farm, u.phone AS farmer_phone, u.blocked AS farmer_blocked "
            "FROM products p JOIN users u ON u.id = p.farmer_id ORDER BY p.id DESC LIMIT 1000"))


def admin_set_product_active(pid, active):
    with conn() as c:
        return c.execute("UPDATE products SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (int(active), pid)).rowcount == 1


def admin_delete_product(pid):
    """Returns the image path of the deleted product, or None if it was not found."""
    with conn() as c:
        r = c.execute("SELECT image FROM products WHERE id = ?", (pid,)).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM products WHERE id = ?", (pid,))
        return r["image"] or ""


def admin_clear_product_image(pid):
    """Returns the old image path, or None if the product does not exist."""
    with conn() as c:
        r = c.execute("SELECT image FROM products WHERE id = ?", (pid,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE products SET image = '', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pid,))
        return r["image"] or ""


def admin_image_rows():
    with conn() as c:
        return _rows(c.execute(
            "SELECT p.id, p.name, p.image, COALESCE(NULLIF(u.farm_name, ''), u.name) AS farm "
            "FROM products p JOIN users u ON u.id = p.farmer_id WHERE p.image != '' ORDER BY p.id DESC"))


def referenced_images():
    with conn() as c:
        return {r["image"].rsplit("/", 1)[-1] for r in c.execute("SELECT image FROM products WHERE image != ''")}


def admin_orders(status=None):
    with conn() as c:
        if status:
            rows = _rows(c.execute(_SELECT_ORDER + " WHERE o.status = ? ORDER BY o.id DESC LIMIT 300", (status,)))
        else:
            rows = _rows(c.execute(_SELECT_ORDER + " ORDER BY o.id DESC LIMIT 300"))
        return _attach_items(c, rows)


def admin_cancel_order(oid):
    """Force-cancel an open order and put the stock back. False if it is already closed."""
    with conn() as c:
        cur = c.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_by = 'admin', otp_nonce = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status IN ('placed','accepted','out_for_delivery')", (oid,))
        if cur.rowcount != 1:
            return False
        for it in c.execute("SELECT product_id, qty FROM order_items WHERE order_id = ?", (oid,)).fetchall():
            if it["product_id"] is not None:
                c.execute("UPDATE products SET qty = ROUND(qty + ?, 3), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                          (it["qty"], it["product_id"]))
        return True

