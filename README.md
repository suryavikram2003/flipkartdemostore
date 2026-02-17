# Simple FastAPI E‑commerce Demo

Minimal e‑commerce demo built with **FastAPI**, **SQLite**, **Jinja2 templates**, and **Tailwind CSS (CDN)**.

It lets you:

- Browse a small product catalog
- Add items to a session‑based cart
- View your cart and subtotal
- Connect to Stripe Checkout for real card payments (or fall back to a simulated checkout page)

> For real payments, use Stripe test keys while developing. Do not paste live keys into source code.

## Requirements

- Python 3.10+ recommended
- `pip` available in your PATH

## Setup

From the project root (`e:\lcurse`):

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate  # PowerShell / cmd on Windows
pip install -r requirements.txt
```

## Running the app

From inside the `backend` directory with the virtualenv activated:

```bash
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000` in your browser.

## Enabling Stripe payments

1. Create a free Stripe account and get your **Secret key** (starts with `sk_test_...` in test mode).
2. Set it as an environment variable before running Uvicorn (PowerShell example):

   ```powershell
   $env:STRIPE_SECRET_KEY="sk_test_your_key_here"
   uvicorn app.main:app --reload
   ```

3. The checkout button on the cart page will now send customers to Stripe Checkout.
4. Stripe will redirect back to `/checkout/success` after payment, which shows the order summary.

If `STRIPE_SECRET_KEY` is **not** set or Stripe fails, the app falls back to the built‑in simulated success page.

## Project structure

- `app/main.py` – FastAPI app, routes, and cart/checkout logic
- `app/models.py` – SQLAlchemy models and SQLite session helpers
- `templates/` – Jinja2 templates (`base.html`, `index.html`, `cart.html`, `checkout_success.html`)
- `static/styles.css` – Optional extra CSS on top of Tailwind CDN

