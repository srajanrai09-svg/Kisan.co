# Local Farmer Market

A marketplace where **farmers sell directly to nearby customers**. Everything on the site is listed by farmers: there are no sample products.

## What it does

| You asked for | How it works |
|---|---|
| Explore first, login at the side | The shop opens straight away for everyone, with no login. **Log in** and **Register** are buttons at the top right and open a side panel. Ordering, the cart and the farmer screens need an account |
| Login for farmer and customer | Register / Log in with mobile number and password. Two roles with separate screens |
| Farmer adds images and price | My products: name, category, price, unit, quantity, photo. Edit price, quantity and photo any time |
| Bill at the end | A bill is shown when the order is placed and again, stamped DELIVERED, after the deal closes. Printable |
| OTP when the customer receives the product | When the farmer taps **Out for delivery**, the customer sees a 6-digit OTP. The farmer must enter it to close the deal |
| Nearer products first | The browser's location (GPS) is used. Products are sorted nearest farm first |
| Search shows nearer first, far last | Searching keeps the same order: nearest farm first, farthest last |
| Search by pincode | Type a pincode: results are measured from that pincode |
| Only farmers' products, real time | No seed data. A new product, price change, order or status change appears on other screens within about a second, without reloading |
| Min and max price for farmers (RAG) | While adding a product the farmer sees the lowest, middle and highest price of similar products |

Plus the earlier stack: Ollama (local AI), FAISS (vector search), SQLite (storage), PostGIS (nearby farms), OSRM (road distance and time), Redis (cache). All optional: if one is not running the app falls back. The admin panel's **System & AI** page shows what is on.

## Run it

```powershell
cd farmer-market-backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
Open **http://localhost:8000/** (or double-click `run.bat`). Use a single worker: live updates are kept in the server's memory.

For AI answers install Ollama and run `ollama pull llama3.2:3b` and `ollama pull nomic-embed-text`. For PostGIS and Redis run `docker compose up -d postgis redis`. Road routing needs a map extract (see `docker-compose.yml`). FAISS is optional: `pip install faiss-cpu` on Python 3.9 to 3.12; without it an exact NumPy search is used.

## Try the whole flow (2 minutes)
1. Window 1: **Register** as a **Farmer**, press *Use my farm's location*, then add a product (try "Tomato", 40, 20 kg, with a photo).
2. Window 2 (a private window): the product is already visible without logging in. Click **Register** (top right) as a **Customer**.
3. Add it to the cart and **Place order**. The bill appears.
4. Farmer: **Orders** → *Accept* → *Out for delivery*.
5. Customer: **My orders** shows the OTP. Tell it to the farmer.
6. Farmer types the OTP → *Complete delivery*. The order is Delivered and the bill is stamped DELIVERED.

## How the farmer price reference works (RAG)
1. **Retrieve**: similar products from other farmers (name similarity, same unit family, converted to your unit, so 500 g prices compare with kg prices) and the nearest row of `data/price_reference.csv`.
2. **Augment**: minimum, middle (median) and maximum are computed from what was retrieved, and your price is checked against them.
3. **Generate**: if Ollama is running it writes a short explanation using only those numbers. Otherwise a template sentence is used. The numbers always come from the data, never from the model.

With 3 or more similar listings the range comes from real listings. With fewer, the indicative range in `data/price_reference.csv` is shown and labelled as a rough guide. **Those reference prices are approximate and not live market data. Edit the file with rates from your own mandi or market.**

## Delivery OTP: how it stays safe
- Created only when the farmer taps *Out for delivery*; only the customer's own screen receives it.
- The OTP is derived from a server secret (`data/secret.key`), so no readable OTP is stored in the database.
- The farmer has 5 tries. After that the customer must press *Get a new OTP*; old OTPs stop working.
- It is also printed in the server console as a simulated SMS. To send real SMS, set `SMS_WEBHOOK_URL` to your own gateway: the server POSTs `{"to": phone, "message": text}`.

## Pincodes
A pincode is placed on the map from farms registered in it. If none is, and internet is available, OpenStreetMap Nominatim is asked once and the answer is remembered. Set `GEOCODE_ONLINE=0` to stay fully offline. If a pincode still cannot be placed, farms with the most similar pincode (most matching leading digits) are shown first and the page says so.

## Privacy and security
- Passwords are stored as salted PBKDF2 hashes. Login is rate limited.
- Browsing products (`GET /products`, `GET /nearby`) is open to guests, with a per-IP rate limit. Everything else (cart, orders, farmer tools, price reference, chat) needs a logged-in session.
- A customer's home location is never stored. A farm's exact coordinates are never sent to other users, only the distance.
- Farmers can change only their own products; orders are visible only to their customer and farmer.
- Prices are always taken from the database when ordering, never from the browser. Stock is reserved in the same step, so the last kilo cannot be sold twice. Cancelling puts stock back.
- Uploaded photos are checked to be real JPEG, PNG or WebP files (max 1.5 MB) and stored under `data/uploads`.
- Text written by farmers is cleaned before it reaches an AI prompt and the assistant is told to ignore instructions inside it.
- Payment is **not** handled online: the customer pays the farmer on delivery.

## Files
- `app/main.py` API · `db.py` SQLite · `geo.py` PostGIS, OSRM, pincodes · `rag.py` customer assistant · `pricing.py` farmer price reference · `security.py` passwords, sessions, OTP · `events.py` live updates
- `static/index.html` the website · `data/faq.txt` what the assistant knows about how the site works · `data/price_reference.csv` indicative prices
- Data is created in `data/` on first run (`market_v2.db`, `uploads/`, `index_v2/`, `secret.key`). Delete `market_v2.db` and `uploads/` to start from an empty market. An older `market.db` or `index/` folder from the previous version is no longer used and can be deleted.

## Before you put this on the internet
Serve it over HTTPS, set `CORS_ORIGINS` to your site's address, back up `data/`, and connect a real SMS gateway. Also decide how disputes are handled: this app records orders and OTP-confirmed delivery, but it does not move money.


## Admin panel (separate, password protected)
Nothing on the public website links to it. It has its own password, its own login cookie and its own pages in `admin_ui/`.

1. Set the password once (it is asked twice, never shown, stored only as a salted hash):
   ```powershell
   python set_admin_password.py
   ```
2. Start the server and open **http://localhost:8000/admin**
3. Pages (each opens in the same tab): Dashboard, Users, Products, Orders, Feedback & complaints, Images, Categories, System & AI, Activity log.

| Page | Control |
|---|---|
| Users | Search, block / unblock (blocking logs the user out everywhere and hides a farmer's listings) |
| Products | Search, remove any product |
| Orders | Filter by status, force-cancel an open order (stock is returned) |
| Feedback & complaints | Read purchase feedback and farmer complaints, summarize comments with local Ollama when available, then reply; replies appear in the related customer/farmer account |
| Images | Replace the home banner, remove a product photo, delete unused photo files |
| Categories | Add categories (with an emoji), change emoji, delete unused ones. Farmers and customers see them immediately |
| System & AI | Service status, rebuild the assistant's search index, test what the assistant retrieves, test the price reference |
| Activity log | Every admin login (also failed ones) and every change, with time and IP |

Forgot the password? Run `python set_admin_password.py` again. Setting it logs every admin out. Session length: `ADMIN_SESSION_HOURS` (default 2).

### How the admin panel and the site are protected
- The admin pages are not in `static/`, so the public site cannot serve them. Without the admin cookie every admin page redirects to the login page and every `/admin-api` call returns 401.
- Login is limited to 5 wrong tries per 15 minutes per address (30 per hour overall). The cookie is HttpOnly and SameSite=Strict (and Secure over HTTPS).
- Changes also need a header that other websites cannot send, and the request's Origin must match. Admin pages use a strict content security policy (no inline scripts).
- **Customers and farmers cannot change the rules by editing the page code.** The browser only sends what the user wants (which product, how many). The server looks up the price, checks the role, checks who owns the order or product, and checks the delivery OTP. Anything extra a user adds to a request is ignored.
- `/docs` and `/openapi.json` are off. Set `ENABLE_DOCS=1` only while developing. `/health` only answers `{"ok": true}`.
- Use HTTPS (for example behind Caddy or nginx) before putting the site on the internet. Run one worker.

## Customer and farmer updates
- Product pages show the description and ratings only after a customer submits feedback for a delivered purchase. Empty feedback stays empty; the app does not create sample ratings or sales.
- **About Kisan.co** counts orders completed with the delivery OTP. It does not count pending, cancelled, or unpaid checkouts as sales.
- Customers can submit one rating per delivered order. Admin replies are visible to that customer and the farmer.
- Farmers can submit complaints and read the market team's replies in **Feedback & help**. Admins can review and reply from **Feedback & complaints** and can block/unblock accounts from **Users**.
- Shop, product detail, cart, orders, About, profile, farmer orders, and farmer help have separate browser-history routes. Product details show other currently listed products below.
- Stock is checked and decremented inside one SQLite write transaction. If several people submit at the same time for the last unit, the first successful transaction gets it and later requests receive an out-of-stock message. This is immediate pay-on-delivery ordering; the app does not yet implement timed holds or online payment confirmation.

## Before deployment: integrations still needed
This archive is a FastAPI server with SQLite, not a Netlify-only static site. Netlify can serve the website, but the Python API needs a separate always-on host or a backend rewrite. Supabase is not connected in this build. To finish that deployment, provide a Supabase project URL and public anon key, a private server-side service key (store it only as a deployment secret), the chosen API host, the Netlify site/repository URL, and any SMS and payment provider details. Never send passwords or secret keys in chat; enter them in the provider's secret settings.

Phone ownership verification at signup and timed `available → reserved → committed` payment holds also need an SMS verification provider and payment provider with server-side webhooks. The existing delivery OTP only confirms handover. Do not count a checkout as a sale until payment/delivery is confirmed.

The current AI feature uses local Ollama for marketplace text answers and price explanations. It does not verify identity or determine that a person is fake. Image similarity can be used as a review signal after choosing a local vision model and testing it with real labeled product photos; it should not automatically accuse or block a person. Admin account blocking and product moderation remain human/admin decisions.

The requested admin password is checked by the setup script for length, mixed case, and a number, then stored as a salted hash. It is not embedded in source code. Since the requested password was shared in this conversation, choose a different private password before using an internet-facing admin panel. Anyone with server/database access or control of the source can ultimately change server-side code; keep deployment and database access limited to trusted maintainers.

