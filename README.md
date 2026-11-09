# Black Coffee (Flask + Stripe)

```
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # fill in real values, see below
flask --app app run --debug
```
Test card: `4242 4242 4242 4242`, any future date, any CVC.

## Security built in
- Server-side prices only — the browser never sends a price, only product IDs.
- CSRF protection (Flask-WTF) on every form that changes state.
- Security headers on every response: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS in production.
- Session cookies: HttpOnly, SameSite=Lax, Secure in production.
- Rate limiting on cart/checkout/admin-login endpoints (Flask-Limiter) against abuse and brute force.
- Stripe webhook signature verified (`STRIPE_WEBHOOK_SECRET`) and idempotent — a resent event never creates a duplicate order.
- Startup refuses to boot in production with a default `SECRET_KEY` or a missing Stripe key.
- The app never sees card numbers — Stripe Checkout is hosted by Stripe, so PCI scope stays minimal.

Still your responsibility: keep `requirements.txt` updated (`pip list --outdated`), never commit `.env`, and rotate any key you ever pasted somewhere public.

## Database: customers + orders
Every completed Stripe checkout is saved by the `/webhook` route into two tables — `customers` and `orders` — via SQLAlchemy.

- **Local dev**: `DATABASE_URL=sqlite:///local.db` (the default) — a file next to `app.py`.
- **Production**: use a real Postgres URL. A SQLite file does **not** survive a restart on most free hosts (the disk is wiped). Free Postgres options: Render's free Postgres (expires after 90 days, then re-create), [Neon](https://neon.tech) or [Supabase](https://supabase.com) (both have a free tier that doesn't expire). Copy the connection string they give you into `DATABASE_URL`.

### Viewing your data
Go to `/admin`, log in, and you'll see every order (date, email, total, items, shipping address) and every customer. Set it up:
```
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-chosen-password'))"
```
Put that output in `ADMIN_PASSWORD_HASH`, and pick any `ADMIN_USERNAME`. Don't put your real password in `.env` — only the hash.

## Deploying — free, with Cloudflare

**Cloudflare Pages does not run Flask** — it only serves static files. For a Python app, host the server itself on a free Python host and let Cloudflare handle your domain, DNS and CDN in front of it.

1. **Push to GitHub** (`.gitignore` already excludes `.env` and the database file).
2. **Host the server — [Render](https://render.com), free tier:**
   - New → Web Service → connect your repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
   - Add environment variables: `SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BASE_URL` (your Render URL, then your final domain), `DATABASE_URL` (a free Postgres from Neon/Supabase — see above), `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, and `FLASK_ENV=production`.
   - Free tier sleeps after inactivity — the first visit after a while takes ~30s to wake up. Fine for starting out.
3. **Point your domain through Cloudflare:**
   - Add your domain to Cloudflare (free plan). Cloudflare gives you new nameservers — set those at your domain registrar.
   - In DNS, add a `CNAME` record: your domain → `your-app.onrender.com`, proxy ON (orange cloud). This gives you Cloudflare's CDN, DDoS protection and free SSL in front of Render.
   - In Render, add the custom domain under Settings → Custom Domains, and update `BASE_URL` to `https://yourdomain.com`.
4. **Stripe webhook, pointed at your real domain:** Dashboard → Developers → Webhooks → Add endpoint → `https://yourdomain.com/webhook`, event `checkout.session.completed`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET` on Render.
5. **Go live:** switch `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` to live values once your Stripe account is activated, and make one real test purchase yourself before sharing the link.

**Quick private preview without deploying anything:** `cloudflared tunnel --url http://localhost:5000` while `flask run` is running locally — gives you a temporary public link, good for showing family/friends, not for a permanent store (link changes every time, your computer must stay on).

## About Strapi
Strapi is a **content management system** (like a headless WordPress) — it is not a payment processor and cannot charge a card by itself. Payments in this project are already fully handled by **Stripe**, directly from Flask; you don't need Strapi for that.

Strapi would only make sense here if you wanted to **edit your product catalog without touching code** — e.g., through an admin screen instead of the `PRODUCTS` dict in `app.py`. That's a separate project: Strapi needs its own Node.js hosting and its own database, and Flask would then fetch the product list from Strapi's API instead of from `PRODUCTS`. Stripe still does the charging either way. Worth doing later if you start changing prices/products often; not required to start selling.

## Everything else
- Prices: `PRODUCTS` / `PLANS` in `app.py`. Currency: `CUR` (USD/EUR, shipping, free-shipping threshold).
- Responsive UI: Bootstrap 5.3 + `static/style.css`. Logo: `static/img/logo.png` (your own silhouette).
- Remove one item / clear cart from `/cart`. Pages: `/story`, `/founder`, `/subscribe`.
