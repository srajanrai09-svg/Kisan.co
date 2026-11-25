"""Settings. Every value can be overridden with an environment variable (or a .env file)."""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE / "data"))
INDEX_DIR = Path(os.getenv("INDEX_DIR", DATA_DIR / "index_v2"))
SQLITE_PATH = os.getenv("SQLITE_PATH", str(DATA_DIR / "market_v2.db"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", DATA_DIR / "uploads"))

# Ollama (local LLM + embeddings)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CHAT_MODEL = os.getenv("CHAT_MODEL", "llama3.2:3b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
VISION_MODEL = os.getenv("VISION_MODEL", "llava:7b")

# Redis cache
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# PostgreSQL + PostGIS (nearby-farm search)
PG_DSN = os.getenv("PG_DSN", "postgresql://market:market@localhost:5432/market")

# OSRM routing server
OSRM_URL = os.getenv("OSRM_URL", "http://localhost:5000")

CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]

# Accounts and orders
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
OTP_MAX_ATTEMPTS = 5
MAX_IMAGE_BYTES = 1_500_000
# Optional: POST {"to": phone, "message": text} to this URL to send real SMS through your own gateway.
SMS_WEBHOOK_URL = os.getenv("SMS_WEBHOOK_URL", "")
# Look up unknown pincodes on OpenStreetMap Nominatim (needs internet). Set to 0 to stay fully offline.
GEOCODE_ONLINE = os.getenv("GEOCODE_ONLINE", "1") == "1"

# Starting categories, copied into the database on first run. After that the admin panel manages them.
DEFAULT_CATEGORIES = [("Vegetables", "🥦"), ("Fruits", "🍎"), ("Dairy", "🥛"), ("Grains", "🌾"), ("Pulses", "🫘"),
                      ("Spices", "🧂"), ("Eggs & Meat", "🥚"), ("Honey & Oils", "🍯"), ("Flowers", "🌼"), ("Other", "🧺")]

# Admin panel
ADMIN_SESSION_HOURS = float(os.getenv("ADMIN_SESSION_HOURS", "2"))
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "0") == "1"   # /docs shows every API route: keep it off on a public server
UNITS = ["kg", "500 g", "250 g", "litre", "500 ml", "dozen", "piece", "bunch", "packet"]

