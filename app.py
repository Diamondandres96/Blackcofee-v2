import os, json, time
from datetime import datetime, timezone
from functools import wraps

import stripe
from dotenv import load_dotenv
from flask import Flask, render_template, request, session, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash

load_dotenv()

# --------------------------------------------------------------------------
# Environment / secrets
# --------------------------------------------------------------------------
IS_PROD = os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER") == "true"

SECRET_KEY = os.getenv("SECRET_KEY", "")
if IS_PROD and (not SECRET_KEY or "change-me" in SECRET_KEY.lower()):
    raise RuntimeError(
        "SECRET_KEY is missing or still the example value. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
if IS_PROD and not STRIPE_SECRET_KEY.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
    raise RuntimeError("STRIPE_SECRET_KEY is missing or does not look like a real Stripe key.")

WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///local.db").replace("postgres://", "postgresql://", 1)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")  # made with werkzeug.security.generate_password_hash

stripe.api_key = STRIPE_SECRET_KEY

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY or "dev-only-change-me"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,          # cookies only over HTTPS once deployed
    MAX_CONTENT_LENGTH=256 * 1024,          # this app never needs uploads; caps body size
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)                     # every POST form must carry a csrf_token
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per hour"])

CUR = {"usd": dict(sym="$", ship=1490, free=8000), "eur": dict(sym="€", ship=1390, free=7500)}
COUNTRIES = ["US","CA","GB","ES","DE","FR","IT","NL","PT","IE","AU","MX","CL","AR","PE","PA","CR","BR"]

# Prices are ALWAYS taken from here (server side), never from the browser.
PRODUCTS = {
  "classic":    dict(name="Juan Valdez Classic", desc="Freeze-dried instant · 150 g · 100 cups", cents=2490, eur=2290, img="classic.jpg", img2="classic_back.jpg", kind="single"),
  "vanicanela": dict(name="Juan Valdez Vanicanela", desc="Vanilla & cinnamon freeze-dried · 95 g · 63 cups", cents=1990, eur=1790, img="vanicanela.jpg", img2="vanicanela_alt.jpg", kind="single"),
  "mesa454":    dict(name="Mesa de los Santos Organic", desc="Certified organic whole bean · 454 g (16 oz)", cents=3490, eur=3190, img="mesa454.jpg", kind="single"),
  "mesa340":    dict(name="Mesa de los Santos Tanager", desc="Roasted Colombian coffee · 340 g (12 oz)", cents=2990, eur=2790, img="mesa340.jpg", kind="single"),
  "wush":       dict(name="Mesa de los Santos Wush", desc="Roasted Colombian coffee · Tangara cyanicollis edition", cents=2990, eur=2790, img="wush.jpg", kind="single"),
  "descaf":     dict(name="Juan Valdez Decaf", desc="Freeze-dried decaf instant · 95 g · 63 cups", cents=2190, eur=1990, img="descaf.jpg", kind="single"),
  "clasico250": dict(name="Juan Valdez Classic 250 g", desc="Freeze-dried instant, resealable doypack · 250 g", cents=3690, eur=3390, img="clasico250.jpg", kind="single"),
  "kit5":       dict(name="Juan Valdez Premium Kit", desc="5 ground coffee sachets, 70 g each: Colina, Cumbre, Volcan, Macizo, Colina", cents=2890, eur=2690, img="kit5.jpg", kind="single"),
  "starter":    dict(name="Colombian Starter Combo", desc="Classic 150 g + Vanicanela 95 g. Save about 10%", cents=3990, eur=3690, img="combo_starter.jpg", kind="combo"),
  "duo":        dict(name="Andes Duo Combo", desc="Organic whole bean 454 g + Classic 150 g. Save about 10%", cents=5490, eur=4990, img="combo_duo.jpg", kind="combo"),
  "full":       dict(name="Full Colombia Box", desc="All five coffees. Save about 10% and ships free", cents=12490, eur=11690, img="combo_full.jpg", kind="combo"),
}
PLANS = {
  "starter-monthly":  dict(name="Starter Box, monthly", desc="Classic 150 g + Vanicanela 95 g every month. Shipping included.", cents=5490, eur=4990, img="combo_starter.jpg", tag=None,
                            perks=["Classic + Vanicanela every month", "Shipping included", "Cancel anytime"]),
  "explorer-monthly": dict(name="Explorer Box, monthly", desc="Organic whole bean 454 g + Classic 150 g every month. Shipping included.", cents=6490, eur=5990, img="combo_duo.jpg", tag="Most popular",
                            perks=["Organic whole bean + Classic every month", "Shipping included", "Cancel anytime"]),
}

# --------------------------------------------------------------------------
# Database: customers + orders
# --------------------------------------------------------------------------
class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, index=True)
    name = db.Column(db.String(200))
    phone = db.Column(db.String(40))
    country = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    orders = db.relationship("Order", backref="customer", lazy=True)

class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    stripe_session_id = db.Column(db.String(120), unique=True, index=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    mode = db.Column(db.String(20))         # "payment" or "subscription"
    currency = db.Column(db.String(10))
    total_cents = db.Column(db.Integer)
    items_json = db.Column(db.Text)
    shipping_json = db.Column(db.Text)
    status = db.Column(db.String(30), default="paid")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

with app.app_context():
    db.create_all()

def save_order(session_obj):
    """Idempotent: Stripe may resend the same webhook event more than once."""
    if Order.query.filter_by(stripe_session_id=session_obj["id"]).first():
        return

    details = session_obj.get("customer_details") or {}
    email = (details.get("email") or "").strip().lower() or None
    shipping = session_obj.get("shipping_details") or (session_obj.get("collected_information") or {}).get("shipping_details") or {}
    country = (shipping.get("address") or {}).get("country")

    customer = None
    if email:
        customer = Customer.query.filter_by(email=email).first()
        if customer:
            customer.name = details.get("name") or customer.name
            customer.phone = details.get("phone") or customer.phone
            customer.country = country or customer.country
        else:
            customer = Customer(email=email, name=details.get("name"), phone=details.get("phone"), country=country)
            db.session.add(customer)
        db.session.flush()

    items = stripe.checkout.Session.list_line_items(session_obj["id"], limit=50)
    db.session.add(Order(
        stripe_session_id=session_obj["id"],
        customer=customer,
        mode=session_obj.get("mode"),
        currency=session_obj.get("currency"),
        total_cents=session_obj.get("amount_total") or 0,
        items_json=json.dumps([dict(name=i["description"], qty=i["quantity"]) for i in items["data"]]),
        shipping_json=json.dumps(shipping) if shipping else None,
    ))
    db.session.commit()

# --------------------------------------------------------------------------
# Security headers (every response)
# --------------------------------------------------------------------------
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if IS_PROD:
        resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "frame-src https://checkout.stripe.com; "
        "connect-src 'self'; "
        "base-uri 'self'; form-action 'self'"
    )
    return resp

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def cur():
    c = session.get("cur", "usd"); return c if c in CUR else "usd"
def unit(p): return p["eur"] if cur() == "eur" else p["cents"]
def money(c): return f"{CUR[cur()]['sym']}{c/100:,.2f}"
app.jinja_env.filters["money"] = money
app.jinja_env.filters["price"] = unit

def cart_lines():
    lines, sub = [], 0
    for pid, qty in session.get("cart", {}).items():
        p = PRODUCTS.get(pid)
        if p and qty > 0:
            u = unit(p); lines.append(dict(p, id=pid, qty=qty, cents=u, total=qty * u)); sub += qty * u
    return lines, sub

def shipping_for(sub):
    c = CUR[cur()]; return 0 if sub == 0 or sub >= c["free"] else c["ship"]

@app.context_processor
def inject():
    return dict(cart_count=sum(session.get("cart", {}).values()), free_over=CUR[cur()]["free"], ship=CUR[cur()]["ship"], cur=cur())

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*a, **kw)
    return wrapper

# --------------------------------------------------------------------------
# Shop routes
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", singles={k:v for k,v in PRODUCTS.items() if v["kind"]=="single"},
                           combos={k:v for k,v in PRODUCTS.items() if v["kind"]=="combo"})

@app.post("/cart/add/<pid>")
@limiter.limit("60 per minute")
def cart_add(pid):
    if pid not in PRODUCTS: abort(404)
    cart = session.get("cart", {}); cart[pid] = min(cart.get(pid, 0) + 1, 20); session["cart"] = cart
    return redirect(url_for("cart"))

@app.post("/cart/set/<pid>")
@limiter.limit("60 per minute")
def cart_set(pid):
    cart = session.get("cart", {}); qty = request.form.get("qty", type=int, default=0)
    if qty is None or qty <= 0: cart.pop(pid, None)
    elif pid in PRODUCTS: cart[pid] = min(qty, 20)
    session["cart"] = cart
    return redirect(url_for("cart"))

@app.get("/cart")
def cart():
    lines, sub = cart_lines(); sh = shipping_for(sub)
    return render_template("cart.html", lines=lines, sub=sub, sh=sh, total=sub + sh)

@app.get("/currency/<c>")
def set_currency(c):
    if c in CUR: session["cur"] = c
    return redirect(request.referrer or url_for("index"))

@app.post("/cart/remove/<pid>")
def cart_remove(pid):
    cart = session.get("cart", {}); cart.pop(pid, None); session["cart"] = cart
    return redirect(url_for("cart"))

@app.post("/cart/clear")
def cart_clear():
    session.pop("cart", None); return redirect(url_for("cart"))

@app.get("/story")
def story(): return render_template("story.html")

@app.post("/checkout")
@limiter.limit("10 per minute")
def checkout():
    lines, sub = cart_lines()
    if not lines: return redirect(url_for("cart"))
    s = stripe.checkout.Session.create(
        mode="payment",
        line_items=[dict(quantity=l["qty"], price_data=dict(currency=cur(), unit_amount=l["cents"],
                    product_data=dict(name=l["name"], description=l["desc"]))) for l in lines],
        shipping_address_collection=dict(allowed_countries=COUNTRIES),
        phone_number_collection=dict(enabled=True),
        shipping_options=[dict(shipping_rate_data=dict(type="fixed_amount",
            fixed_amount=dict(amount=shipping_for(sub), currency=cur()),
            display_name="International tracked shipping",
            delivery_estimate=dict(minimum=dict(unit="business_day", value=5), maximum=dict(unit="business_day", value=10))))],
        success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/cart")
    return redirect(s.url, code=303)

@app.get("/subscribe")
def subscribe(): return render_template("subscribe.html", plans=PLANS)

@app.post("/subscribe/<plan>")
@limiter.limit("10 per minute")
def subscribe_checkout(plan):
    p = PLANS.get(plan) or abort(404)
    s = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[dict(quantity=1, price_data=dict(currency=cur(), unit_amount=unit(p),
                    recurring=dict(interval="month"), product_data=dict(name=p["name"], description=p["desc"])))],
        shipping_address_collection=dict(allowed_countries=COUNTRIES),
        phone_number_collection=dict(enabled=True),
        success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/subscribe")
    return redirect(s.url, code=303)

@app.get("/success")
def success():
    session.pop("cart", None)
    return render_template("success.html")

@app.post("/webhook")
@csrf.exempt  # Stripe can't send a CSRF token; the signature check below is what verifies it
@limiter.limit("120 per minute")
def webhook():
    try:
        ev = stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""), WEBHOOK_SECRET)
    except Exception:
        abort(400)
    if ev["type"] == "checkout.session.completed":
        save_order(ev["data"]["object"])
    return "", 200

# --------------------------------------------------------------------------
# Admin: view customers and orders (password-protected)
# --------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def admin_login():
    error = None
    if request.method == "POST":
        u, p = request.form.get("username", ""), request.form.get("password", "")
        if ADMIN_USERNAME and ADMIN_PASSWORD_HASH and u == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, p):
            session.clear(); session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_orders"))
        error = "Invalid username or password."
    return render_template("admin_login.html", error=error)

@app.get("/admin/logout")
def admin_logout():
    session.pop("is_admin", None); return redirect(url_for("admin_login"))

@app.get("/admin")
@admin_required
def admin_orders():
    orders = Order.query.order_by(Order.created_at.desc()).limit(200).all()
    return render_template("admin_orders.html", orders=orders, json=json)

@app.get("/admin/customers")
@admin_required
def admin_customers():
    customers = Customer.query.order_by(Customer.created_at.desc()).limit(500).all()
    return render_template("admin_customers.html", customers=customers)

if __name__ == "__main__":
    app.run(debug=not IS_PROD)
