"""Local Farmer Market API.

Run:  uvicorn app.main:app --port 8000        (use ONE worker: live updates are kept in memory)
Then open http://localhost:8000/ for the website, or http://localhost:8000/docs for the API.
"""
import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Literal, Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import admin, config, db, events, geo, images, notify, pricing, rag, security
from .cache import cache
from .textutil import clean_text

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

PHONE_RE = re.compile(r"^[6-9]\d{9}$")
UNIT_CHARS = re.compile(r"^[A-Za-z0-9 .]{1,20}$")


@asynccontextmanager
async def lifespan(app):
    db.init()
    db.purge_sessions()
    geo.sync_postgis()
    rag.load_or_build()
    log.info("Cache backend: %s", cache.backend)
    yield


# /docs and /openapi.json list every API route, so they are off unless you set ENABLE_DOCS=1
app = FastAPI(title="Local Farmer Market API", lifespan=lifespan,
              docs_url="/docs" if config.ENABLE_DOCS else None, redoc_url=None,
              openapi_url="/openapi.json" if config.ENABLE_DOCS else None)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


# ------------------------------------------------------------------ helpers
def norm_phone(v):
    d = re.sub(r"[\s\-]", "", str(v))
    d = re.sub(r"^(\+91|91|0)(?=\d{10}$)", "", d)
    if not PHONE_RE.match(d):
        raise ValueError("Enter a valid 10-digit mobile number")
    return d


def check_pincode(v):
    v = str(v).strip()
    if not geo.PINCODE_RE.match(v):
        raise ValueError("Enter a valid 6-digit pincode")
    return v


def ts(s):
    """SQLite stores UTC as 'YYYY-MM-DD HH:MM:SS'. Send ISO so the browser shows local time."""
    return s.replace(" ", "T") + "Z" if s else None


def changed(*kinds):
    events.bump(*kinds)
    cache.delete_prefix("nearby:")


def current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in.")
    user = db.user_by_token_hash(security.token_hash(authorization[7:].strip()))
    if not user:
        raise HTTPException(401, "Your session has expired. Please log in again.")
    if user.get("blocked"):
        raise HTTPException(403, "This account has been blocked. Contact the market admin.")
    return user


def optional_user(authorization: Optional[str] = Header(default=None)):
    """Browsing is open to everyone. Returns the logged-in user, or None for a guest."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    u = db.user_by_token_hash(security.token_hash(authorization[7:].strip()))
    return None if u and u.get("blocked") else u


def guest_limit(request: Request, user):
    """Guests are rate limited per IP, because a pincode search can trigger an online lookup."""
    if user is None:
        ip = request.client.host if request.client else "?"
        if security.limiter.blocked(f"browse:{ip}", 120, 60):
            raise HTTPException(429, "Too many searches. Please wait a minute.")
        security.limiter.hit(f"browse:{ip}", 60)


def farmer_only(user=Depends(current_user)):
    if user["role"] != "farmer":
        raise HTTPException(403, "Only farmers can do this.")
    return user


def customer_only(user=Depends(current_user)):
    if user["role"] != "customer":
        raise HTTPException(403, "Only customers can do this.")
    return user


def public_user(u):
    return {k: u[k] for k in ("id", "role", "name", "phone", "farm_name", "area", "address", "pincode", "lat", "lng")}


def product_view(p, distance=None, mine=False):
    d = {"id": p["id"], "name": p["name"], "category": p["category"], "price": p["price"], "unit": p["unit"],
         "qty": p["qty"], "organic": p["organic"], "description": p["description"],
         "image": p["image"] or None, "farm_id": p["farm_id"], "farm": p["farm"], "area": p["area"],
         "pincode": p["pincode"], "distance_km": distance, "created_at": ts(p["created_at"])}
    if mine:
        d["active"] = p["active"]
    return d  # a farm's exact coordinates are never sent to other users


def bill_no(o):
    return f"FM{o['created_at'][:10].replace('-', '')}-{o['id']:05d}"


def order_view(o, viewer):
    is_customer = viewer["id"] == o["customer_id"]
    d = {
        "id": o["id"], "bill_no": bill_no(o), "status": o["status"], "total": o["total"], "items": o["items"],
        "address": o["address"], "pincode": o["pincode"], "note": o["note"], "cancelled_by": o["cancelled_by"],
        "created_at": ts(o["created_at"]), "updated_at": ts(o["updated_at"]), "delivered_at": ts(o["delivered_at"]),
        "farmer": {"name": o["farm_name"] or o["farmer_name"], "phone": o["farmer_phone"], "area": o["farm_area"]},
        "customer": {"name": o["customer_name"], "phone": o["customer_phone"]},
    }
    if o["status"] == "out_for_delivery":
        if is_customer:
            d["otp"] = security.otp_for(o["id"], o["otp_nonce"])  # only the customer ever receives the OTP
        else:
            d["otp_attempts_left"] = max(0, config.OTP_MAX_ATTEMPTS - o["otp_attempts"])
    return d


def load_order(oid, user):
    o = db.get_order(oid)
    if not o or user["id"] not in (o["customer_id"], o["farmer_id"]):
        raise HTTPException(404, "Order not found.")  # same answer whether it exists or not
    return o


# ------------------------------------------------------------------ models
class RegisterIn(BaseModel):
    role: Literal["farmer", "customer"]
    name: str = Field(min_length=2, max_length=60)
    phone: str
    password: str = Field(min_length=8, max_length=100)
    pincode: str
    address: str = Field(default="", max_length=200)
    area: str = Field(default="", max_length=60)
    farm_name: Optional[str] = Field(default=None, max_length=80)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return norm_phone(v)

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        return check_pincode(v)


class LoginIn(BaseModel):
    phone: str
    password: str = Field(min_length=1, max_length=100)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return norm_phone(v)


class MeIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    farm_name: Optional[str] = Field(default=None, max_length=80)
    address: Optional[str] = Field(default=None, max_length=200)
    area: Optional[str] = Field(default=None, max_length=60)
    pincode: Optional[str] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        return check_pincode(v) if v is not None else v


class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    category: str
    price: float = Field(gt=0, le=100000)
    unit: str = Field(min_length=1, max_length=20)
    qty: float = Field(ge=0, le=100000)
    organic: bool = False
    description: str = Field(default="", max_length=200)
    image_data: Optional[str] = Field(default=None, max_length=3_000_000)

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v not in db.category_names():
            raise ValueError("Choose a category from the list")
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        v = v.strip()
        if not UNIT_CHARS.match(v):
            raise ValueError("Unit can use letters, numbers and spaces only (for example kg or 500 g)")
        return v


class ProductPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    category: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0, le=100000)
    unit: Optional[str] = Field(default=None, min_length=1, max_length=20)
    qty: Optional[float] = Field(default=None, ge=0, le=100000)
    organic: Optional[bool] = None
    description: Optional[str] = Field(default=None, max_length=200)
    active: Optional[bool] = None
    image_data: Optional[str] = Field(default=None, max_length=3_000_000)
    remove_image: bool = False

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v is not None and v not in db.category_names():
            raise ValueError("Choose a category from the list")
        return v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not UNIT_CHARS.match(v):
            raise ValueError("Unit can use letters, numbers and spaces only")
        return v


class OrderLine(BaseModel):
    product_id: int
    qty: float = Field(gt=0, le=10000)


class OrderIn(BaseModel):
    items: list[OrderLine] = Field(min_length=1, max_length=50)
    address: str = Field(min_length=5, max_length=200)
    pincode: str
    note: str = Field(default="", max_length=200)

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v):
        return check_pincode(v)


class OtpIn(BaseModel):
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class PurchaseFeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class ComplaintIn(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=10, max_length=2000)


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)


# ------------------------------------------------------------------ accounts
@app.get("/meta")
def meta():
    return {"categories": db.category_names(), "emojis": db.category_emojis(), "units": config.UNITS,
            "otp_max_attempts": config.OTP_MAX_ATTEMPTS, "sold": db.delivered_units_sold()}


@app.get("/about")
def about():
    return {"sold": db.delivered_units_sold(), "statement": "Sales count includes completed, OTP-confirmed deliveries only."}


def _start_session(user_id):
    token = security.new_token()
    db.create_session(user_id, security.token_hash(token), config.SESSION_DAYS)
    return token


@app.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, background: BackgroundTasks):
    ip = request.client.host if request.client else "?"
    if security.limiter.blocked(f"reg:{ip}", 20, 3600):
        raise HTTPException(429, "Too many sign-ups from this device. Try again later.")
    security.limiter.hit(f"reg:{ip}", 3600)

    lat, lng = body.lat, body.lng
    if body.role == "farmer":
        if (lat is None or lng is None):
            point = geo.resolve_pincode(body.pincode)
            if not point:
                raise HTTPException(422, "Farm location is required. Press 'Use my farm's location'.")
            lat, lng = point["lat"], point["lng"]
        if not (body.farm_name or "").strip():
            raise HTTPException(422, "Enter your farm name.")
    else:
        lat = lng = None  # a customer's home location is never stored
    try:
        uid = db.create_user(body.role, clean_text(body.name, 60), body.phone, security.hash_password(body.password),
                             clean_text(body.farm_name or "", 80) or None, clean_text(body.area, 60),
                             clean_text(body.address, 200), body.pincode, lat, lng)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This mobile number is already registered. Please log in.")
    if body.role == "farmer":
        db.learn_pincode(body.pincode, lat, lng)
        background.add_task(geo.sync_postgis)
        changed("products")
    return {"token": _start_session(uid), "user": public_user(db.user_by_id(uid))}


@app.post("/auth/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    kp, ki = f"login:{body.phone}", f"loginip:{ip}"
    if security.limiter.blocked(kp, 8, 600) or security.limiter.blocked(ki, 40, 600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
    u = db.user_by_phone(body.phone)
    if not u:
        security.burn_time(body.password)
    if not u or not security.verify_password(body.password, u["pw_hash"]):
        security.limiter.hit(kp, 600)
        security.limiter.hit(ki, 600)
        raise HTTPException(401, "Wrong mobile number or password.")
    if u.get("blocked"):
        raise HTTPException(403, "This account has been blocked. Contact the market admin.")
    security.limiter.reset(kp)
    return {"token": _start_session(u["id"]), "user": public_user(db.user_by_id(u["id"]))}


@app.post("/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        db.delete_session(security.token_hash(authorization[7:].strip()))
    return {"ok": True}


@app.get("/me")
def me(user=Depends(current_user)):
    return {"user": public_user(user)}


@app.patch("/me")
def update_me(body: MeIn, background: BackgroundTasks, user=Depends(current_user)):
    f = body.model_dump(exclude_none=True)
    for k, n in (("name", 60), ("farm_name", 80), ("area", 60), ("address", 200)):
        if k in f:
            f[k] = clean_text(f[k], n)
    if user["role"] == "customer":
        f.pop("lat", None)
        f.pop("lng", None)
        f.pop("farm_name", None)
    db.update_user(user["id"], **f)
    if user["role"] == "farmer" and ("lat" in f or "lng" in f or "farm_name" in f or "area" in f):
        new = db.user_by_id(user["id"])
        if new["lat"] is not None:
            db.learn_pincode(new["pincode"], new["lat"], new["lng"])
        background.add_task(geo.sync_postgis)
        changed("products")
        rag.schedule_reindex()
    return {"user": public_user(db.user_by_id(user["id"]))}


# ------------------------------------------------------------------ products
@app.get("/products")
def list_products(
    request: Request,
    q: Optional[str] = Query(None, max_length=60),
    category: Optional[str] = Query(None, max_length=30),
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lng: Optional[float] = Query(None, ge=-180, le=180),
    pincode: Optional[str] = Query(None, max_length=6),
    limit: int = Query(200, ge=1, le=500),
    user=Depends(optional_user),
):
    """Public: no login needed. Products nearest first. Where 'near' is measured from, in order: a pincode, the GPS position, or the
    pincode on the user's profile. Searching by name keeps the same order (nearest farm first, farthest last)."""
    guest_limit(request, user)
    origin = None          # {"lat","lng","label","source"}
    prefix = None          # pincode to rank by shared leading digits when it cannot be placed on the map
    if pincode:
        try:
            pin = check_pincode(pincode)
        except ValueError as e:
            raise HTTPException(422, str(e))
        point = geo.resolve_pincode(pin)
        if point:
            origin = {"lat": point["lat"], "lng": point["lng"], "label": f"pincode {pin}", "source": "pincode"}
        else:
            prefix = pin
    elif lat is not None and lng is not None:
        origin = {"lat": lat, "lng": lng, "label": "your location", "source": "gps"}
    elif user and geo.PINCODE_RE.match(user.get("pincode") or ""):
        point = geo.resolve_pincode(user["pincode"], online=False)
        if point:
            origin = {"lat": point["lat"], "lng": point["lng"], "label": f"your pincode {user['pincode']}", "source": "profile"}

    rows = db.products(q=q, category=category)
    qw = (q or "").lower().strip()
    out = []
    for p in rows:
        d = None
        if origin and p["lat"] is not None:
            d = round(geo.haversine_km(origin["lat"], origin["lng"], p["lat"], p["lng"]), 1)
        rel = 2 if qw and p["name"].lower().startswith(qw) else 1 if qw and qw in p["name"].lower() else 0
        shared = 0
        if prefix:
            while shared < 6 and p["pincode"][shared : shared + 1] == prefix[shared]:
                shared += 1
        out.append((d, -rel, -shared, p["price"], p))
    if origin:
        out.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0, t[1], t[3]))
    elif prefix:
        out.sort(key=lambda t: (t[2], t[1], t[3]))
    # without any location: newest first, which is the order db.products() already returns
    items = [product_view(p, d) for d, _, _, _, p in out[:limit]]
    for it, (_, _, shared, _, _) in zip(items, out[:limit]):
        if prefix:
            it["pincode_match"] = shared
    return {"products": items, "count": len(out), "origin": origin,
            "pincode_unresolved": prefix, "ordered_by": "distance" if origin else "pincode" if prefix else "newest"}


@app.get("/my/products")
def my_products(user=Depends(farmer_only)):
    return {"products": [product_view(p, mine=True) for p in db.my_products(user["id"])]}


def _save_image_or_422(data):
    try:
        return images.save(data)
    except images.ImageError as e:
        raise HTTPException(422, str(e))


@app.post("/products", status_code=201)
def add_product(body: ProductIn, user=Depends(farmer_only)):
    path = _save_image_or_422(body.image_data) if body.image_data else ""
    pid = db.add_product(user["id"], clean_text(body.name, 60), body.category, round(body.price, 2), body.unit,
                         round(body.qty, 3), body.organic, clean_text(body.description, 200), path)
    changed("products")
    rag.schedule_reindex()
    return {"product": product_view(db.get_product(pid), mine=True)}


def _own_product(pid, user):
    p = db.get_product(pid)
    if not p or p["farmer_id"] != user["id"]:
        raise HTTPException(404, "Product not found.")
    return p


@app.patch("/products/{pid}")
def edit_product(pid: int, body: ProductPatch, user=Depends(farmer_only)):
    old = _own_product(pid, user)
    f = body.model_dump(exclude_unset=True, exclude={"image_data", "remove_image"})
    if "name" in f:
        f["name"] = clean_text(f["name"], 60)
    if "description" in f:
        f["description"] = clean_text(f["description"], 200)
    if "price" in f:
        f["price"] = round(f["price"], 2)
    if "qty" in f:
        f["qty"] = round(f["qty"], 3)
    if body.image_data:
        f["image"] = _save_image_or_422(body.image_data)
    elif body.remove_image:
        f["image"] = ""
    db.update_product(pid, user["id"], **f)
    if "image" in f:
        images.remove(old["image"])
    changed("products")
    rag.schedule_reindex()
    return {"product": product_view(db.get_product(pid), mine=True)}


@app.delete("/products/{pid}")
def remove_product(pid: int, user=Depends(farmer_only)):
    image = db.delete_product(pid, user["id"])
    if image is None:
        raise HTTPException(404, "Product not found.")
    images.remove(image)
    changed("products")
    rag.schedule_reindex()
    return {"ok": True}


@app.get("/price-reference")
def price_reference(
    name: str = Query(min_length=2, max_length=60),
    unit: str = Query("kg", max_length=20),
    price: Optional[float] = Query(None, gt=0, le=100000),
    ai: bool = False,
    user=Depends(farmer_only),
):
    """Lowest and highest price for similar products, to help a farmer price their own."""
    return pricing.price_reference(name, unit, user["id"], price, ai)


# ------------------------------------------------------------------ nearby farms and routes
@app.get("/nearby")
def nearby(
    request: Request,
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(50, ge=0, le=20000, description="0 means any distance"),
    limit: int = Query(20, ge=1, le=50),
    roads: bool = True,
    user=Depends(optional_user),
):
    """Public. Farms with stock near a point, nearest first, with road distance and delivery time."""
    guest_limit(request, user)
    farms = geo.nearby_farms(lat, lng, radius_km, limit, roads)
    return {"farms": [{k: v for k, v in f.items() if k not in ("lat", "lng")} for f in farms]}


@app.get("/route")
def route(lat1: float, lng1: float, lat2: float, lng2: float, user=Depends(current_user)):
    """Driving route between two points (distance, minutes and a GeoJSON line)."""
    return geo.route(lat1, lng1, lat2, lng2)


# ------------------------------------------------------------------ orders
@app.post("/orders", status_code=201)
def place_order(body: OrderIn, user=Depends(customer_only)):
    merged = {}
    for line in body.items:
        merged[line.product_id] = merged.get(line.product_id, 0) + line.qty
    try:
        ids = db.create_orders(user["id"], list(merged.items()), clean_text(body.address, 200), body.pincode,
                               clean_text(body.note, 200))
    except db.OrderError as e:
        raise HTTPException(409, str(e))
    if not user["address"]:
        db.update_user(user["id"], address=clean_text(body.address, 200), pincode=body.pincode)
    changed("products", "orders")
    rag.schedule_reindex()
    return {"orders": [order_view(db.get_order(i), user) for i in ids]}


@app.get("/orders")
def my_orders(user=Depends(current_user)):
    return {"orders": [order_view(o, user) for o in db.list_orders(user["role"], user["id"])]}


@app.get("/orders/{oid}")
def one_order(oid: int, user=Depends(current_user)):
    return {"order": order_view(load_order(oid, user), user)}


def _refreshed(oid, user):
    events.bump("orders")
    return {"order": order_view(db.get_order(oid), user)}


@app.post("/orders/{oid}/accept")
def accept_order(oid: int, user=Depends(farmer_only)):
    load_order(oid, user)
    if not db.transition(oid, "farmer_id", user["id"], ("placed",), "accepted"):
        raise HTTPException(409, "This order can no longer be accepted.")
    return _refreshed(oid, user)


def _send_otp(o):
    otp = security.otp_for(o["id"], o["otp_nonce"])
    notify.send_sms(o["customer_phone"], f"Your Local Farmer Market delivery OTP for order {bill_no(o)} is {otp}. "
                                         f"Share it with the farmer only when you have received your order.")


@app.post("/orders/{oid}/dispatch")
def dispatch_order(oid: int, user=Depends(farmer_only)):
    """The farmer sets off with the order. The customer now sees the OTP, to be given at handover."""
    load_order(oid, user)
    if not db.transition(oid, "farmer_id", user["id"], ("accepted",), "out_for_delivery",
                         otp_nonce=security.new_nonce(), otp_attempts=0):
        raise HTTPException(409, "Accept the order first, or it has already been sent out.")
    _send_otp(db.get_order(oid))
    return _refreshed(oid, user)


@app.post("/orders/{oid}/complete")
def complete_order(oid: int, body: OtpIn, user=Depends(farmer_only)):
    """Closes the deal. Only works with the OTP the customer received."""
    o = load_order(oid, user)
    if o["status"] == "delivered":
        raise HTTPException(409, "This order is already closed.")
    if o["status"] != "out_for_delivery":
        raise HTTPException(409, "Send the order out for delivery first.")
    if not db.use_otp_attempt(oid, user["id"]):
        raise HTTPException(423, "Too many wrong OTPs. Ask the customer to request a new OTP.")
    fresh = db.get_order(oid)
    if not security.check_otp(oid, o["otp_nonce"], body.otp):
        left = max(0, config.OTP_MAX_ATTEMPTS - fresh["otp_attempts"])
        raise HTTPException(400, f"Wrong OTP. {left} {'try' if left == 1 else 'tries'} left.")
    if not db.transition(oid, "farmer_id", user["id"], ("out_for_delivery",), "delivered",
                         delivered_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), otp_nonce=None):
        raise HTTPException(409, "This order is already closed.")
    return _refreshed(oid, user)


@app.post("/orders/{oid}/new-otp")
def new_otp(oid: int, user=Depends(customer_only)):
    """Customer asks for a fresh OTP (for example after the farmer used up the tries)."""
    load_order(oid, user)
    if not db.transition(oid, "customer_id", user["id"], ("out_for_delivery",), "out_for_delivery",
                         otp_nonce=security.new_nonce(), otp_attempts=0):
        raise HTTPException(409, "There is no delivery in progress for this order.")
    _send_otp(db.get_order(oid))
    return _refreshed(oid, user)


@app.post("/orders/{oid}/cancel")
def cancel_order(oid: int, user=Depends(current_user)):
    o = load_order(oid, user)
    if user["id"] == o["customer_id"]:
        ok = db.cancel_order(oid, "customer_id", user["id"], ("placed", "accepted"), "customer")
    else:
        ok = db.cancel_order(oid, "farmer_id", user["id"], ("placed", "accepted", "out_for_delivery"), "farmer")
    if not ok:
        raise HTTPException(409, "This order can no longer be cancelled.")
    changed("products", "orders")
    rag.schedule_reindex()
    return {"order": order_view(db.get_order(oid), user)}


@app.post("/orders/{oid}/feedback")
def purchase_feedback(oid: int, body: PurchaseFeedbackIn, user=Depends(customer_only)):
    result = db.save_purchase_feedback(oid, user["id"], body.rating, clean_text(body.comment, 1000))
    if result == "ineligible":
        raise HTTPException(409, "Feedback is available after this purchase is delivered.")
    if result == "exists":
        raise HTTPException(409, "Feedback has already been submitted for this purchase.")
    return {"ok": True}


@app.get("/feedback/mine")
def my_feedback(user=Depends(customer_only)):
    return {"feedback": db.customer_feedback(user["id"])}


@app.get("/farmer/feedback")
def farmer_feedback(user=Depends(farmer_only)):
    return {"feedback": db.farmer_feedback(user["id"]), "complaints": db.farmer_complaints(user["id"])}


@app.post("/farmer/complaints", status_code=201)
def farmer_complaint(body: ComplaintIn, user=Depends(farmer_only)):
    db.submit_complaint(user["id"], clean_text(body.subject, 120), clean_text(body.message, 2000))
    return {"ok": True}


# ------------------------------------------------------------------ assistant
@app.post("/chat")
def chat(body: ChatIn, user=Depends(current_user)):
    """Ask the RAG assistant. Send lat and lng (or have a pincode on your profile) to ask what is near you."""
    if security.limiter.blocked(f"chat:{user['id']}", 20, 60):
        raise HTTPException(429, "Please slow down a little.")
    security.limiter.hit(f"chat:{user['id']}", 60)
    lat, lng = body.lat, body.lng
    if lat is None and geo.PINCODE_RE.match(user.get("pincode") or ""):
        point = geo.resolve_pincode(user["pincode"], online=False)
        if point:
            lat, lng = point["lat"], point["lng"]
    return rag.answer(body.question, lat, lng)


# ------------------------------------------------------------------ live updates
@app.get("/events")
async def live_events(request: Request):
    """Server-sent events. The page reloads its lists whenever a counter changes. No private data is sent."""
    if _sse["open"] >= 300:
        raise HTTPException(503, "Too many live connections. Try again shortly.")

    async def stream():
        _sse["open"] += 1
        try:
            async for chunk in _stream(request):
                yield chunk
        finally:
            _sse["open"] -= 1

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


_sse = {"open": 0}


async def _stream(request):
    last = events.snapshot()
    yield "retry: 3000\n\n"
    yield f"data: {json.dumps(last)}\n\n"
    idle = 0
    while True:
        await asyncio.sleep(1)
        if await request.is_disconnected():
            break
        cur = events.snapshot()
        if cur != last:
            last, idle = cur, 0
            yield f"data: {json.dumps(cur)}\n\n"
        else:
            idle += 1
            if idle >= 15:
                idle = 0
                yield ": keepalive\n\n"


# ------------------------------------------------------------------ health
@app.get("/health")
def health():
    """Public and minimal. Service details are in the admin panel (System page)."""
    try:
        with db.conn() as c:
            c.execute("SELECT 1")
        return {"ok": True}
    except Exception:
        raise HTTPException(503, "Database unavailable")


app.include_router(admin.router)


# Product photos, then the website itself at "/". Registered last so the API routes win.
config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=config.UPLOAD_DIR), name="uploads")
if (config.BASE / "static").exists():
    app.mount("/", StaticFiles(directory=config.BASE / "static", html=True), name="site")

