"""Flask web app — Phase 12 shell.

Visualization-only. Zero input capability.
All data flows Telegram -> DB -> Flask.

Routes:
    GET /                     Redirect to /spendly/
    GET /spendly/             Main overview (totals, charts, recent)
    GET /dashboard            Main overview (totals, charts, recent)
    GET /expenses             Paginated expense list with filters
    GET /reports              Monthly report timeline
    GET /reports/<month>      Single month full report
    GET /insights             Insights tab
    GET /merchants            Merchant memory viewer (read-only)
    GET /chat                  Gemini-powered NL chat (read-only queries)
    POST /api/chat             JSON: chat message -> AI reply
    GET /api/dashboard        JSON: dashboard summary
    GET /api/expenses         JSON: expense list (paginated + filtered)
    GET /api/reports          JSON: monthly reports list
    GET /api/reports/<month>  JSON: single report detail
    GET /api/insights         JSON: recent insights
    GET /api/anomalies        JSON: recent anomaly alerts
    GET /api/merchants        JSON: merchant memory
    GET /health               JSON: liveness probe
    GET /api/health           JSON: deep observability (DB, WAL, tables, uptime)
    GET /export/csv           Download expenses as CSV
    GET /export/pdf           Download expenses as PDF
"""

from __future__ import annotations

from datetime import date
from functools import wraps
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request

from spendly.core.config import settings
from spendly.core.logger import get_logger
from spendly.web.db import (
    get_anomaly_alerts,
    get_archive_summary,
    get_archive_years,
    get_archived_expenses,
    get_category_detail,
    get_dashboard_summary,
    get_expense_count,
    get_expenses,
    get_merchant_memory,
    get_monthly_report_detail,
    get_monthly_reports_list,
    get_projects_summary,
    get_recent_insights,
    get_timeline_summary,
    get_token_usage,
    get_user_id,
)

log = get_logger(__name__)


# ── Export helpers ─────────────────────────────────────────────────────────────


def _export_label(date_from: str, date_to: str, category: str | None) -> str:
    """Human label for the export, e.g. 'April 2025 - Food'."""
    try:
        from datetime import date as _date

        d0 = _date.fromisoformat(date_from)
        d1 = _date.fromisoformat(date_to)
        if d0.year == d1.year and d0.month == d1.month:
            label = d0.strftime("%B %Y")
        else:
            label = f"{d0.strftime('%-d %b')} - {d1.strftime('%-d %b %Y')}"
    except ValueError:
        label = f"{date_from} to {date_to}"
    return f"{label} - {category}" if category else label


def _compact(date_from: str, date_to: str) -> str:
    """Compact date range for filename, e.g. '2025-04' or '2025-03-12_2025-04-04'."""
    try:
        from datetime import date as _date

        d0 = _date.fromisoformat(date_from)
        d1 = _date.fromisoformat(date_to)
        if d0.year == d1.year and d0.month == d1.month:
            return d0.strftime("%Y-%m")
    except ValueError:
        pass
    return f"{date_from}_{date_to}"


# ── App factory ────────────────────────────────────────────────────────────────


def create_app() -> Flask:
    """Create and configure the Flask application."""
    import time
    from datetime import UTC, datetime

    # ── Error Monitoring ──────────────────────────────────────────────────────
    if settings.sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=1.0,
        )
        log.info("Sentry monitoring active (Web)")

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_SORT_KEYS"] = False
    app.config["STARTUP_TS"] = datetime.now(UTC).isoformat()

    # ── Proxy support ─────────────────────────────────────────────────────────
    # Trust headers from GCP load balancers/proxies (Standard for Gunicorn)
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # ── Request timing middleware ──────────────────────────────────────────────
    @app.before_request
    def _start_timer():
        from flask import g

        g._request_start = time.monotonic()

    @app.after_request
    def _log_request(response):
        from flask import g

        elapsed = time.monotonic() - getattr(g, "_request_start", time.monotonic())
        log.debug(
            "HTTP",
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "ms": round(elapsed * 1000, 1),
            },
        )
        return response

    _register_routes(app)

    # Redirect root to spendly dashboard
    @app.route("/")
    def root_redirect():
        return redirect("/spendly/")

    @app.route("/favicon.ico")
    def favicon():
        from flask import send_from_directory
        import os
        return send_from_directory(
            os.path.join(app.root_path, "static"),
            "icon-192.png",
            mimetype="image/png"
        )

    _register_error_handlers(app)

    log.info("Flask web app created", extra={"startup": app.config["STARTUP_TS"]})
    return app


# ── User guard ─────────────────────────────────────────────────────────────────


def _require_user(f: Any) -> Any:
    """Decorator that injects user_id, returning 503 if user not found in DB."""

    @wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        uid = get_user_id()
        if uid is None:
            return (
                jsonify({"error": "No data yet — send your first expense via Telegram."})
                if "/api/" in request.path
                else render_template(
                    "error.html",
                    title="No Data Yet",
                    message="Send your first expense via Telegram to get started.",
                ),
                200,
            )
        return f(uid, *args, **kwargs)

    wrapper.__name__ = f.__name__
    return wrapper


# ── Route registration ─────────────────────────────────────────────────────────


def _register_routes(app: Flask) -> None:
    from flask import Blueprint

    web = Blueprint("web", __name__, url_prefix="/spendly")

    # ── HTML pages ─────────────────────────────────────────────────────────────

    @web.get("/")
    @_require_user
    def home(user_id: int):
        return dashboard(user_id)

    @web.get("/dashboard")
    @_require_user
    def dashboard(user_id: int):
        data = get_dashboard_summary(user_id)
        return render_template("dashboard.html", **data)

    @web.get("/expenses")
    def expenses_redirect():
        from flask import url_for
        return redirect(url_for("web.transactions"), code=301)

    @web.get("/transactions")
    @_require_user
    def transactions(user_id: int):
        from spendly.web.db import get_transactions, get_transaction_count
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        category = request.args.get("category")
        tag = request.args.get("tag")
        txn_type = request.args.get("type") or "all"
        page = max(1, int(request.args.get("page", 1)))
        per_page = 50

        rows = get_transactions(
            user_id,
            date_from=date_from,
            date_to=date_to,
            category=category,
            tag=tag,
            txn_type=txn_type,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        total = get_transaction_count(
            user_id,
            date_from=date_from,
            date_to=date_to,
            category=category,
            tag=tag,
            txn_type=txn_type,
        )
        pages = (total + per_page - 1) // per_page

        return render_template(
            "transactions.html",
            transactions=rows,
            page=page,
            pages=pages,
            total=total,
            date_from=date_from,
            date_to=date_to,
            category=category,
            tag=tag,
            txn_type=txn_type,
        )

    @web.get("/reports")
    @_require_user
    def reports(user_id: int):
        report_list = get_monthly_reports_list(user_id)
        return render_template("reports.html", reports=report_list)

    @web.get("/reports/<month>")
    @_require_user
    def report_detail(user_id: int, month: str):
        report = get_monthly_report_detail(user_id, month)
        if not report:
            return render_template(
                "error.html", title="Not Found", message=f"No report found for {month}."
            ), 404
        return render_template("report_detail.html", report=report, month=month)

    @web.get("/insights")
    @_require_user
    def insights(user_id: int):
        insights_list = get_recent_insights(user_id, limit=20)
        anomalies = get_anomaly_alerts(user_id, limit=10)
        return render_template("insights.html", insights=insights_list, anomalies=anomalies)

    @web.get("/merchants")
    @_require_user
    def merchants(user_id: int):
        merchant_list = get_merchant_memory(user_id)
        return render_template("merchants.html", merchants=merchant_list)

    @web.get("/projects")
    @_require_user
    def projects(user_id: int):
        projects_list = get_projects_summary(user_id)
        return render_template("projects.html", projects=projects_list)

    @web.get("/subscriptions")
    @_require_user
    def subscriptions(user_id: int):
        from spendly.web.db import get_recurring_subscriptions

        subs = get_recurring_subscriptions(user_id)
        return render_template("subscriptions.html", subscriptions=subs)

    @web.get("/timeline")
    @_require_user
    def timeline(user_id: int):
        months = get_timeline_summary(user_id)
        # Pass set of months that have reports so template can show link indicator
        report_months = {r["month"] for r in get_monthly_reports_list(user_id)}
        return render_template("timeline.html", months=months, report_months=report_months)

    @web.get("/category/<category_name>")
    @_require_user
    def category_detail(user_id: int, category_name: str):
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        detail = get_category_detail(user_id, category_name, date_from, date_to)
        if detail["count"] == 0 and not date_from:
            return render_template(
                "error.html",
                title="No Data",
                message=f"No expenses found for category '{category_name}'.",
            ), 404
        return render_template("category_detail.html", detail=detail)

    @web.get("/archives/<int:year>")
    @_require_user
    def archives(user_id: int, year: int):
        today = date.today()
        if year < 2020 or year >= today.year:
            return render_template(
                "error.html",
                title="Invalid Year",
                message=f"Archive year must be between 2020 and {today.year - 1}.",
            ), 400
        expenses = get_archived_expenses(user_id, year)
        summary = get_archive_summary(user_id, year)
        avail_years = get_archive_years()
        return render_template(
            "archives.html",
            year=year,
            expenses=expenses,
            current_year=today.year,
            summary=summary,
            avail_years=avail_years,
        )

    @web.get("/chat")
    @_require_user
    def chat(user_id: int):
        return render_template("chat.html")

    # ── Export ────────────────────────────────────────────────────────────────

    @web.get("/export/csv")
    @_require_user
    def export_csv(user_id: int):
        """Stream a CSV download for the requested period / category."""
        from flask import Response

        from spendly.utils.export import generate_csv

        date_from = request.args.get("date_from") or date.today().replace(day=1).isoformat()
        date_to = request.args.get("date_to") or date.today().isoformat()
        category = request.args.get("category") or None
        tag = request.args.get("tag") or None

        expenses = get_expenses(
            user_id,
            date_from=date_from,
            date_to=date_to,
            category=category,
            tag=tag,
            limit=10_000,
            offset=0,
        )

        label = _export_label(date_from, date_to, category)
        if tag and not category:
            label = f"{label} · {tag}"
        csv_bytes = generate_csv(expenses, month_label=label)

        safe_cat = f"_{category.lower()}" if category else ""
        compact = _compact(date_from, date_to)
        filename = f"spendly_{compact}{safe_cat}.csv"

        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @web.get("/export/pdf")
    @_require_user
    def export_pdf(user_id: int):
        """Stream a PDF download for the requested period / category."""
        from flask import Response

        from spendly.utils.export import generate_pdf

        date_from = request.args.get("date_from") or date.today().replace(day=1).isoformat()
        date_to = request.args.get("date_to") or date.today().isoformat()
        category = request.args.get("category") or None
        tag = request.args.get("tag") or None

        expenses = get_expenses(
            user_id,
            date_from=date_from,
            date_to=date_to,
            category=category,
            tag=tag,
            limit=10_000,
            offset=0,
        )

        label = _export_label(date_from, date_to, category)
        if tag and not category:
            label = f"{label} · {tag}"
        pdf_bytes = generate_pdf(
            expenses, month_label=label, monthly_budget=settings.monthly_budget
        )

        safe_cat = f"_{category.lower()}" if category else ""
        compact = _compact(date_from, date_to)
        filename = f"spendly_{compact}{safe_cat}.pdf"

        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── JSON API ───────────────────────────────────────────────────────────────

    @web.get("/api/timeline")
    @_require_user
    def api_timeline(user_id: int):
        return jsonify(get_timeline_summary(user_id))

    @web.get("/api/category/<category_name>")
    @_require_user
    def api_category(user_id: int, category_name: str):
        date_from = request.args.get("from")
        date_to = request.args.get("to")
        return jsonify(get_category_detail(user_id, category_name, date_from, date_to))

    @web.post("/api/chat")
    @_require_user
    def api_chat(user_id: int):
        """Process a natural-language query from the web chat UI."""
        from spendly.web.chat import handle_chat

        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        history = body.get("history") or []
        if not message:
            return jsonify({"reply": "What would you like to know?", "ok": True}), 200
        result = handle_chat(message, history)
        return jsonify(result), 200 if result["ok"] else 500

    @web.get("/api/dashboard")
    @_require_user
    def api_dashboard(user_id: int):
        return jsonify(get_dashboard_summary(user_id))

    @web.get("/api/dashboard/charts")
    @_require_user
    def api_dashboard_charts(user_id: int):
        """Unified endpoint for all dashboard chart data."""
        from spendly.web.db import get_history_data, get_sankey_data

        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        history = get_history_data(user_id, date_from=date_from, date_to=date_to)
        sankey = get_sankey_data(user_id)
        return jsonify({"history": history, "sankey": sankey})

    @web.get("/api/expenses")
    @_require_user
    def api_expenses(user_id: int):
        # Kept for backward compatibility, returns transactions instead
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        category = request.args.get("category")
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, int(request.args.get("per_page", 50)))

        from spendly.web.db import get_transactions, get_transaction_count
        rows = get_transactions(
            user_id, date_from, date_to, category, limit=per_page, offset=(page - 1) * per_page
        )
        total = get_transaction_count(user_id, date_from, date_to, category)
        return jsonify({"expenses": rows, "total": total, "page": page, "per_page": per_page})

    @web.get("/api/transactions")
    @_require_user
    def api_transactions(user_id: int):
        from spendly.web.db import get_transactions, get_transaction_count
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        category = request.args.get("category")
        txn_type = request.args.get("type") or "all"
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, int(request.args.get("per_page", 50)))

        rows = get_transactions(
            user_id, date_from, date_to, category, txn_type=txn_type, limit=per_page, offset=(page - 1) * per_page
        )
        total = get_transaction_count(user_id, date_from, date_to, category, txn_type=txn_type)
        return jsonify({"transactions": rows, "total": total, "page": page, "per_page": per_page})

    @web.get("/api/reports")
    @_require_user
    def api_reports(user_id: int):
        return jsonify(get_monthly_reports_list(user_id))

    @web.get("/api/reports/<month>")
    @_require_user
    def api_report_detail(user_id: int, month: str):
        report = get_monthly_report_detail(user_id, month)
        if not report:
            return jsonify({"error": f"No report for {month}"}), 404
        return jsonify(report)

    @web.post("/api/subscriptions/toggle")
    @_require_user
    def api_subscriptions_toggle(user_id: int):
        data = request.get_json(silent=True) or {}
        sub_id = data.get("subscription_id")
        active = data.get("active")
        txn_type = data.get("transaction_type", "expense")
        
        if sub_id is None or active is None:
            return jsonify({"error": "Missing subscription_id or active state"}), 400
            
        import sqlite3
        from spendly.core.config import settings
        
        table = "recurring_expenses" if txn_type == "expense" else "recurring_incomes"
        
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                f"UPDATE {table} SET is_active = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
                (1 if active else 0, sub_id, user_id)
            )
            conn.commit()
            
        return jsonify({"ok": True})

    @web.get("/api/insights")
    @_require_user
    def api_insights(user_id: int):
        limit = min(50, int(request.args.get("limit", 10)))
        return jsonify(get_recent_insights(user_id, limit=limit))

    @web.get("/api/anomalies")
    @_require_user
    def api_anomalies(user_id: int):
        return jsonify(get_anomaly_alerts(user_id))

    @web.get("/api/merchants")
    @_require_user
    def api_merchants(user_id: int):
        return jsonify(get_merchant_memory(user_id))

    @web.get("/api/archives/<int:year>")
    @_require_user
    def api_archives(user_id: int, year: int):
        if year < 2020 or year > date.today().year:
            return jsonify({"error": "Invalid year"}), 400
        expenses = get_archived_expenses(user_id, year)
        summary = get_archive_summary(user_id, year)
        return jsonify({"expenses": expenses, "summary": summary})

    @web.get("/api/archives/years")
    @_require_user
    def api_archive_years(user_id: int):
        return jsonify(get_archive_years())


    @web.get("/health")
    def health():
        """Lightweight liveness probe — always fast, no DB calls."""
        uid = get_user_id()
        return jsonify(
            {
                "status": "ok",
                "db": str(settings.db_path),
                "user": uid,
            }
        )

    @web.get("/api/health")
    def api_health():
        """Deep observability endpoint — DB integrity, WAL, archive years.

        Runs a synchronous DB integrity check inline.
        Response shape:
          {
            "status":       "ok" | "degraded",
            "db_path":      str,
            "db_tables":    int,
            "wal_active":   bool,
            "db_ok":        bool,
            "user_id":      int | None,
            "budget":       float,
            "archive_years": [int],
            "ts":           str   (ISO 8601 UTC)
          }
        """
        import sqlite3
        from datetime import UTC, datetime

        from spendly.db.archive import list_archive_years

        ts = datetime.now(UTC).isoformat()

        # Synchronous DB integrity check (Flask is sync)
        db_tables = 0
        wal_active = False
        db_ok = False
        try:
            conn = sqlite3.connect(str(settings.db_path))
            row = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            db_tables = row[0] if row else 0
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            wal_active = (mode[0] == "wal") if mode else False
            conn.close()
            db_ok = db_tables >= 14
        except Exception:
            log.error("Health check DB error", exc_info=True)

        uid = get_user_id()

        status = "ok" if db_ok else "degraded"
        return jsonify(
            {
                "status": status,
                "db_path": str(settings.db_path),
                "db_tables": db_tables,
                "wal_active": wal_active,
                "db_ok": db_ok,
                "user_id": uid,
                "budget": settings.monthly_budget,
                "archive_years": list_archive_years(),
                "ts": ts,
            }
        ), 200 if status == "ok" else 503

    @web.get("/api/stats/tokens")
    @_require_user
    def api_token_usage(user_id: int):
        return jsonify(get_token_usage(user_id))

    app.register_blueprint(web)


# ── Error handlers ─────────────────────────────────────────────────────────────


def _register_error_handlers(app: Flask) -> None:

    @app.errorhandler(404)
    def not_found(e: Any):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return render_template("error.html", title="Not Found", message=str(e)), 404

    @app.errorhandler(500)
    def server_error(e: Any):
        log.error("Internal server error", exc_info=True)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Internal server error"}), 500
        return render_template("error.html", title="Error", message="Something went wrong."), 500
