from pathlib import Path
from typing import Dict, Optional
import logging
import os

import stripe
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .models import (
    Base,
    Order,
    OrderItem,
    Product,
    User,
    ensure_schema,
    get_engine,
    get_session,
)

logger = logging.getLogger("simple_shop")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Stripe configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def create_app() -> FastAPI:
    app = FastAPI(title="Simple E-commerce")

    # Session middleware
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET_KEY", "change-me-in-production"),
    )

    # Static files and templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # ---------- Error handlers ----------
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        logger.warning("HTTP error %s at %s: %s", exc.status_code, request.url.path, exc.detail)
        status_code = exc.status_code or 500
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "status_code": status_code, "message": exc.detail or "Something went wrong."},
            status_code=status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error at %s", request.url.path)
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "status_code": 500, "message": "Internal server error. Please try again later."},
            status_code=500,
        )

    # Database setup
    engine = get_engine()
    ensure_schema()

    with get_session() as db:
        seed_products(db)

    def _get_cart_count(request: Request) -> int:
        cart: Dict[str, int] = request.session.get("cart", {})
        return sum(cart.values())

    def _get_current_user(db, request: Request) -> Optional[User]:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        return db.query(User).get(user_id)

    # ---------- Routes ----------
    @app.get("/")
    async def home(request: Request):
        q = request.query_params.get("q") or ""
        selected_category = request.query_params.get("category") or ""
        price_band = request.query_params.get("price") or ""

        price_ranges = {"low": (0, 500), "mid": (500, 1000), "high": (1000, 5000)}

        with get_session() as db:
            query = db.query(Product)
            if selected_category:
                query = query.filter(Product.category == selected_category)
            if q:
                query = query.filter(Product.name.ilike(f"%{q}%"))
            if price_band in price_ranges:
                min_p, max_p = price_ranges[price_band]
                query = query.filter(Product.price >= min_p, Product.price <= max_p)

            products = query.all()
            raw_categories = db.query(Product.category).filter(Product.category.isnot(None)).distinct().all()
            categories = sorted({row[0] for row in raw_categories})

        cart_count = _get_cart_count(request)
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "products": products,
                "cart_count": cart_count,
                "categories": categories,
                "selected_category": selected_category,
                "search_query": q,
                "price_band": price_band,
            },
        )


    @app.post("/cart/add/{product_id}")
    async def add_to_cart(request: Request, product_id: int):
        with get_session() as db:
            product = db.query(Product).get(product_id)
            if product is None:
                return RedirectResponse(url="/", status_code=303)

        cart: Dict[str, int] = request.session.get("cart", {})
        key = str(product_id)
        cart[key] = cart.get(key, 0) + 1
        request.session["cart"] = cart

        return RedirectResponse(url="/", status_code=303)

    @app.get("/cart")
    async def view_cart(request: Request):
        cart: Dict[str, int] = request.session.get("cart", {})
        product_ids = [int(pid) for pid in cart.keys()]

        with get_session() as db:
            products = (
                db.query(Product).filter(Product.id.in_(product_ids)).all()
                if product_ids
                else []
            )

        # Build cart items with totals
        items = []
        subtotal = 0.0
        product_map = {p.id: p for p in products}
        for pid_str, qty in cart.items():
            pid = int(pid_str)
            product = product_map.get(pid)
            if not product:
                continue
            line_total = product.price * qty
            subtotal += line_total
            items.append(
                {
                    "product": product,
                    "quantity": qty,
                    "line_total": line_total,
                }
            )

        cart_count = _get_cart_count(request)

        return templates.TemplateResponse(
            "cart.html",
            {
                "request": request,
                "items": items,
                "subtotal": subtotal,
                "cart_count": cart_count,
            },
        )

    @app.post("/cart/clear")
    async def clear_cart(request: Request):
        request.session["cart"] = {}
        return RedirectResponse(url="/cart", status_code=303)

    @app.post("/checkout")
    async def checkout(request: Request):
        cart: Dict[str, int] = request.session.get("cart", {})
        if not cart:
            return RedirectResponse(url="/cart", status_code=303)

        product_ids = [int(pid) for pid in cart.keys()]
        order_id: Optional[int] = None
        with get_session() as db:
            products = (
                db.query(Product).filter(Product.id.in_(product_ids)).all()
                if product_ids
                else []
            )

            items = []
            subtotal = 0.0
            product_map = {p.id: p for p in products}

            # Data for session storage (simple dicts) and Stripe line items
            order_items_for_session = []
            stripe_line_items = []

            for pid_str, qty in cart.items():
                pid = int(pid_str)
                product = product_map.get(pid)
                if not product:
                    continue
                line_total = product.price * qty
                subtotal += line_total

                items.append(
                    {
                        "product": product,
                        "quantity": qty,
                        "line_total": line_total,
                    }
                )

                order_items_for_session.append(
                    {
                        "product": {
                            "name": product.name,
                        },
                        "quantity": qty,
                        "line_total": line_total,
                    }
                )

                if STRIPE_SECRET_KEY:
                    stripe_line_items.append(
                        {
                            "price_data": {
                                "currency": "inr",
                                "product_data": {
                                    "name": product.name,
                                },
                                "unit_amount": int(product.price * 100),
                            },
                            "quantity": qty,
                        }
                    )

            # Persist order and order items
            user = _get_current_user(db, request)
            order_status = "pending" if STRIPE_SECRET_KEY else "paid"
            order = Order(
                user_id=user.id if user else None,
                total_amount=subtotal,
                status=order_status,
            )
            db.add(order)
            db.flush()  # get order.id

            for pid_str, qty in cart.items():
                pid = int(pid_str)
                product = product_map.get(pid)
                if not product:
                    continue
                line_total = product.price * qty
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=qty,
                    unit_price=product.price,
                    line_total=line_total,
                )
                db.add(order_item)

            db.commit()
            # capture order id before session closes to avoid DetachedInstanceError
            order_id = order.id

        # Store last order summary in session for the success page
        request.session["last_order"] = {
            "order_id": order_id,
            "items": order_items_for_session,
            "subtotal": subtotal,
        }

        # Clear cart after "checkout"
        request.session["cart"] = {}

        # If Stripe is configured, create a Checkout Session and redirect there.
        if STRIPE_SECRET_KEY and stripe_line_items:
            try:
                success_url = str(
                    request.url_for("checkout_success")
                ) + "?session_id={CHECKOUT_SESSION_ID}"
                cancel_url = str(request.url_for("view_cart"))

                checkout_session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=stripe_line_items,
                    success_url=success_url,
                    cancel_url=cancel_url,
                    metadata={"order_id": str(order.id)},
                )

                return RedirectResponse(
                    url=checkout_session.url, status_code=303
                )
            except Exception:
                # If Stripe fails for any reason, fall back to local success page.
                pass

        # Fallback: render local success page without external payment
        return templates.TemplateResponse(
            "checkout_success.html",
            {
                "request": request,
                "items": order_items_for_session,
                "subtotal": subtotal,
            },
        )

    @app.get("/checkout/success", name="checkout_success")
    async def checkout_success(request: Request, session_id: Optional[str] = None):
        last_order = request.session.get("last_order")
        if not last_order:
            return RedirectResponse(url="/", status_code=303)

        order_id = last_order.get("order_id")

        # Mark order as paid if we have an ID
        if order_id:
            with get_session() as db:
                order = db.query(Order).get(order_id)
                if order and order.status != "paid":
                    order.status = "paid"
                    db.commit()

        return templates.TemplateResponse(
            "checkout_success.html",
            {
                "request": request,
                "items": last_order.get("items", []),
                "subtotal": last_order.get("subtotal", 0.0),
            },
        )

    @app.get("/profile")
    async def profile(request: Request):
        with get_session() as db:
            user = _get_current_user(db, request)

        cart_count = _get_cart_count(request)

        return templates.TemplateResponse(
            "profile.html",
            {
                "request": request,
                "user": user,
                "cart_count": cart_count,
            },
        )

    @app.post("/profile")
    async def update_profile(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
    ):
        with get_session() as db:
            existing = (
                db.query(User)
                .filter(User.email == email.strip().lower())
                .first()
            )
            if existing:
                existing.name = name.strip()
                user = existing
            else:
                user = User(
                    name=name.strip(),
                    email=email.strip().lower(),
                )
                db.add(user)
            db.commit()
            db.refresh(user)

        request.session["user_id"] = user.id

        return RedirectResponse(url="/dashboard", status_code=303)

    @app.get("/dashboard")
    async def dashboard(request: Request):
        with get_session() as db:
            user = _get_current_user(db, request)
            if not user:
                return RedirectResponse(url="/profile", status_code=303)

            db_orders = (
                db.query(Order)
                .filter(Order.user_id == user.id)
                .order_by(Order.created_at.desc())
                .all()
            )

            # Build plain Python structures so we don't depend on an open DB session in templates.
            orders = []
            for order in db_orders:
                lines = []
                for item in order.items:
                    product_name = item.product.name if item.product else "Item"
                    lines.append(
                        {
                            "product_name": product_name,
                            "quantity": item.quantity,
                            "line_total": item.line_total,
                        }
                    )

                orders.append(
                    {
                        "id": order.id,
                        "created_at": order.created_at,
                        "total_amount": order.total_amount,
                        "status": order.status,
                        "lines": lines,
                    }
                )

        cart_count = _get_cart_count(request)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "orders": orders,
                "cart_count": cart_count,
            },
        )

    return app


def seed_products(db_session):
    seed_data = [
        {
            "name": "Minimalist T-Shirt",
            "description": "Soft cotton tee in a clean, modern cut.",
            "price": 19.99,
            "image_url": "https://images.pexels.com/photos/1002638/pexels-photo-1002638.jpeg",
            "category": "Fashion",
        },
        {
            "name": "Everyday Backpack",
            "description": "Versatile backpack with padded laptop sleeve.",
            "price": 59.99,
            "image_url": "https://images.pexels.com/photos/374592/pexels-photo-374592.jpeg",
            "category": "Bags & Luggage",
        },
        {
            "name": "Wireless Headphones",
            "description": "Noise-cancelling over-ear headphones with long battery life.",
            "price": 129.99,
            "image_url": "https://images.pexels.com/photos/3394664/pexels-photo-3394664.jpeg",
            "category": "Electronics",
        },
        {
            "name": "Android Smartphone",
            "description": "6.5\" display, 5G ready, all-day battery life.",
            "price": 249.99,
            "image_url": "https://images.pexels.com/photos/6078121/pexels-photo-6078121.jpeg",
            "category": "Mobiles & Tablets",
        },
        {
            "name": "Ultrabook Laptop",
            "description": "Thin and light laptop for work and entertainment.",
            "price": 799.0,
            "image_url": "https://images.pexels.com/photos/18105/pexels-photo.jpg",
            "category": "Laptops",
        },
        {
            "name": "Home Coffee Maker",
            "description": "Brew rich coffee at home with one-touch control.",
            "price": 89.99,
            "image_url": "https://images.pexels.com/photos/302899/pexels-photo-302899.jpeg",
            "category": "Home & Kitchen",
        },
        {
            "name": "Yoga Mat Pro",
            "description": "Non-slip yoga mat with extra cushioning.",
            "price": 29.99,
            "image_url": "https://images.pexels.com/photos/3823086/pexels-photo-3823086.jpeg",
            "category": "Sports & Fitness",
        },
        {
            "name": "Skincare Essentials Kit",
            "description": "Cleanser, toner and moisturizer for daily care.",
            "price": 39.99,
            "image_url": "https://images.pexels.com/photos/3738364/pexels-photo-3738364.jpeg",
            "category": "Beauty & Personal Care",
        },
        {
            "name": "LED Desk Lamp",
            "description": "Adjustable desk lamp with warm and cool modes.",
            "price": 24.99,
            "image_url": "https://images.pexels.com/photos/8132693/pexels-photo-8132693.jpeg",
            "category": "Home & Lighting",
        },
        {
            "name": "Bluetooth Speaker",
            "description": "Portable speaker with deep bass and 12h playtime.",
            "price": 49.99,
            "image_url": "https://images.pexels.com/photos/63703/pexels-photo-63703.jpeg",
            "category": "Electronics",
        },
        {
            "name": "Running Shoes",
            "description": "Lightweight running shoes for everyday training.",
            "price": 64.99,
            "image_url": "https://images.pexels.com/photos/2529148/pexels-photo-2529148.jpeg",
            "category": "Footwear",
        },
        {
            "name": "Study Chair",
            "description": "Ergonomic chair with lumbar support for long study hours.",
            "price": 119.0,
            "image_url": "https://images.pexels.com/photos/6964079/pexels-photo-6964079.jpeg",
            "category": "Furniture",
        },
    ]

    existing_names = {name for (name,) in db_session.query(Product.name).all()}
    for data in seed_data:
        if data["name"] in existing_names:
            continue
        db_session.add(Product(**data))
    db_session.commit()


# ---------- Entry point for Render ----------
app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
