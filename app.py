import csv
import io
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from database import init_db, db
from scheduler import (
    start_scheduler, stop_scheduler,
    get_warm_day, emails_for_day, sent_today, send_now, RAMP,
)
from mailer import test_connection

logging.basicConfig(level=logging.INFO)
templates = Jinja2Templates(directory="templates")

# ── Auth ──────────────────────────────────────────────────────────────────────
WARMY_PASSWORD = os.environ.get("WARMY_PASSWORD", "changeme")
_sessions: set[str] = set()

def _check_auth(session: str | None) -> bool:
    return bool(session and session in _sessions)

def _auth_required(request: Request, session: str | None = Cookie(default=None)):
    if not _check_auth(session):
        raise HTTPException(status_code=307, headers={"Location": "/login"})

# 1×1 transparent GIF
PIXEL_GIF = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
    b"\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00"
    b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)

# CSV column aliases → internal field name
CSV_MAP = {
    "email": "email", "e-mail": "email",
    "first_name": "first_name", "first name": "first_name", "firstname": "first_name", "fname": "first_name", "name": "first_name",
    "last_name": "last_name", "last name": "last_name", "lastname": "last_name", "lname": "last_name", "surname": "last_name",
    "company": "company", "organization": "company", "organisation": "company", "org": "company", "business": "company",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Warmy", lifespan=lifespan)


# ── Login ─────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Warmy — Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#fff;border:1px solid #e0e0e0;border-radius:14px;padding:36px;width:340px;display:flex;flex-direction:column;gap:16px}}
h1{{font-size:20px;font-weight:700}}
label{{font-size:12px;font-weight:500;color:#374151;display:flex;flex-direction:column;gap:4px}}
input{{border:1px solid #d1d5db;border-radius:6px;padding:8px 10px;font-size:13px;width:100%;outline:none}}
input:focus{{border-color:#6366f1}}
button{{background:#4f46e5;color:#fff;border:none;border-radius:6px;padding:9px;font-size:13px;font-weight:600;cursor:pointer}}
.err{{color:#dc2626;font-size:12px}}
</style></head>
<body><div class="card">
<h1>🔥 Warmy</h1>
{error}
<form method="post" action="/login" style="display:flex;flex-direction:column;gap:12px">
<label>Password<input type="password" name="password" autofocus required /></label>
<button type="submit">Sign in</button>
</form></div></body></html>"""

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML.format(error="")

@app.post("/login")
async def login(password: str = Form(...)):
    if secrets.compare_digest(password, WARMY_PASSWORD):
        token = secrets.token_urlsafe(32)
        _sessions.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=60*60*24*30)
        return resp
    return HTMLResponse(LOGIN_HTML.format(error='<p class="err">Incorrect password.</p>'), status_code=401)

@app.post("/logout")
async def logout(session: str | None = Cookie(default=None)):
    if session:
        _sessions.discard(session)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ── Tracking pixel ────────────────────────────────────────────────────────────

@app.get("/t/{token}")
async def tracking_pixel(token: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM open_tokens WHERE token=? AND opened=0", (token,)
        ).fetchone()
        if row:
            from database import db as _db
            conn.execute(
                "UPDATE open_tokens SET opened=1, opened_at=datetime('now') WHERE token=?",
                (token,),
            )
            conn.execute(
                "INSERT INTO lead_events (lead_id, campaign_id, event_type, step_number, detail) VALUES (?,?,?,?,?)",
                (row["lead_id"], row["campaign_id"], "opened", row["step_number"], ""),
            )
    return Response(content=PIXEL_GIF, media_type="image/gif")


# ── Warm-up dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: str | None = Cookie(default=None)):
    if not _check_auth(session):
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()
        partners = conn.execute("SELECT * FROM partners ORDER BY created_at DESC").fetchall()
        recent = conn.execute("SELECT * FROM sent_emails ORDER BY sent_at DESC LIMIT 20").fetchall()
        total_sent = conn.execute("SELECT COUNT(*) as c FROM sent_emails").fetchone()["c"]
        settings = conn.execute("SELECT * FROM app_settings WHERE id=1").fetchone()

    day = get_warm_day()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "seed": seed,
        "partners": partners,
        "recent": recent,
        "total_sent": total_sent,
        "today_sent": sent_today(),
        "today_target": emails_for_day(day) if day else 0,
        "warm_day": day,
        "ramp": RAMP,
        "settings": settings,
    })


@app.post("/seed")
async def save_seed(
    name: str = Form(...),
    email: str = Form(...),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_user: str = Form(...),
    smtp_pass: str = Form(...),
    use_tls: int = Form(1),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
):
    with db() as conn:
        conn.execute("DELETE FROM seed_email")
        conn.execute(
            "INSERT INTO seed_email (name,email,smtp_host,smtp_port,smtp_user,smtp_pass,use_tls,imap_host,imap_port) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, email, smtp_host, smtp_port, smtp_user, smtp_pass, use_tls, imap_host, imap_port),
        )
    return RedirectResponse("/", status_code=303)


@app.post("/seed/test")
async def test_seed():
    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()
    if not seed:
        return {"ok": False, "error": "No seed email saved yet — click Save first."}
    return test_connection(dict(seed))


@app.post("/settings")
async def save_settings(tracking_base_url: str = Form("")):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (id, tracking_base_url) VALUES (1, ?)",
            (tracking_base_url.strip(),),
        )
    return RedirectResponse("/", status_code=303)


@app.post("/partners")
async def add_partner(email: str = Form(...)):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO partners (email) VALUES (?)", (email.strip().lower(),))
    return RedirectResponse("/", status_code=303)


@app.post("/partners/{pid}/delete")
async def delete_partner(pid: int):
    with db() as conn:
        conn.execute("DELETE FROM partners WHERE id=?", (pid,))
    return RedirectResponse("/", status_code=303)


@app.post("/partners/{pid}/toggle")
async def toggle_partner(pid: int):
    with db() as conn:
        conn.execute("UPDATE partners SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (pid,))
    return RedirectResponse("/", status_code=303)


@app.post("/warm/start")
async def warm_start():
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO warm_start (id) VALUES (1)")
    return RedirectResponse("/", status_code=303)


@app.post("/warm/reset")
async def warm_reset():
    with db() as conn:
        conn.execute("DELETE FROM warm_start")
        conn.execute("DELETE FROM sent_emails")
    return RedirectResponse("/", status_code=303)


@app.post("/warm/send-now")
async def warm_send_now(count: int = Form(1)):
    return send_now(count)


# ── Sendy ─────────────────────────────────────────────────────────────────────

@app.get("/sendy", response_class=HTMLResponse)
async def sendy_index(request: Request, session: str | None = Cookie(default=None)):
    if not _check_auth(session):
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        campaigns = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        stats = {}
        for c in campaigns:
            row = conn.execute("""
                SELECT
                  COUNT(*) as total,
                  SUM(CASE WHEN status='pending'  THEN 1 ELSE 0 END) as pending,
                  SUM(CASE WHEN status='active'   THEN 1 ELSE 0 END) as active,
                  SUM(CASE WHEN status='replied'  THEN 1 ELSE 0 END) as replied,
                  SUM(CASE WHEN status='unsubscribed' THEN 1 ELSE 0 END) as unsubscribed,
                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                  SUM(CASE WHEN status='bounced'  THEN 1 ELSE 0 END) as bounced
                FROM leads WHERE campaign_id=?
            """, (c["id"],)).fetchone()
            stats[c["id"]] = dict(row)

    return templates.TemplateResponse("sendy.html", {
        "request": request,
        "campaigns": campaigns,
        "stats": stats,
    })


@app.post("/sendy/campaigns")
async def create_campaign(
    name: str = Form(...),
    daily_limit: int = Form(30),
):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, daily_limit) VALUES (?, ?)",
            (name.strip(), daily_limit),
        )
    return RedirectResponse(f"/sendy/campaigns/{cur.lastrowid}", status_code=303)


@app.get("/sendy/campaigns/{cid}", response_class=HTMLResponse)
async def campaign_detail(request: Request, cid: int, session: str | None = Cookie(default=None)):
    if not _check_auth(session):
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        steps = conn.execute(
            "SELECT * FROM campaign_steps WHERE campaign_id=? ORDER BY step_number", (cid,)
        ).fetchall()
        leads = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? ORDER BY created_at DESC LIMIT 200", (cid,)
        ).fetchall()
        stats = conn.execute("""
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN status='pending'      THEN 1 ELSE 0 END) as pending,
              SUM(CASE WHEN status='active'       THEN 1 ELSE 0 END) as active,
              SUM(CASE WHEN status='replied'      THEN 1 ELSE 0 END) as replied,
              SUM(CASE WHEN status='unsubscribed' THEN 1 ELSE 0 END) as unsubscribed,
              SUM(CASE WHEN status='completed'    THEN 1 ELSE 0 END) as completed,
              SUM(CASE WHEN status='bounced'      THEN 1 ELSE 0 END) as bounced
            FROM leads WHERE campaign_id=?
        """, (cid,)).fetchone()
        opens = conn.execute(
            "SELECT COUNT(*) as c FROM open_tokens WHERE campaign_id=? AND opened=1", (cid,)
        ).fetchone()["c"]
        sent_total = conn.execute(
            "SELECT COUNT(*) as c FROM lead_events WHERE campaign_id=? AND event_type='sent'", (cid,)
        ).fetchone()["c"]

    return templates.TemplateResponse("campaign.html", {
        "request": request,
        "campaign": campaign,
        "steps": steps,
        "leads": leads,
        "stats": dict(stats),
        "opens": opens,
        "sent_total": sent_total,
    })


@app.post("/sendy/campaigns/{cid}/steps")
async def add_step(
    cid: int,
    subject: str = Form(...),
    body: str = Form(...),
    delay_days: int = Form(0),
):
    with db() as conn:
        last = conn.execute(
            "SELECT COALESCE(MAX(step_number),0) as n FROM campaign_steps WHERE campaign_id=?", (cid,)
        ).fetchone()["n"]
        step_num = last + 1
        if step_num == 1:
            delay_days = 0  # First step always immediate
        conn.execute(
            "INSERT INTO campaign_steps (campaign_id, step_number, delay_days, subject, body) VALUES (?,?,?,?,?)",
            (cid, step_num, delay_days, subject.strip(), body.strip()),
        )
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/steps/{sid}/update")
async def update_step(
    cid: int, sid: int,
    subject: str = Form(...),
    body: str = Form(...),
    delay_days: int = Form(0),
):
    with db() as conn:
        step = conn.execute("SELECT * FROM campaign_steps WHERE id=? AND campaign_id=?", (sid, cid)).fetchone()
        if step and step["step_number"] == 1:
            delay_days = 0
        conn.execute(
            "UPDATE campaign_steps SET subject=?, body=?, delay_days=? WHERE id=? AND campaign_id=?",
            (subject.strip(), body.strip(), delay_days, sid, cid),
        )
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/steps/{sid}/delete")
async def delete_step(cid: int, sid: int):
    with db() as conn:
        conn.execute("DELETE FROM campaign_steps WHERE id=? AND campaign_id=?", (sid, cid))
        # Re-number remaining steps
        steps = conn.execute(
            "SELECT id FROM campaign_steps WHERE campaign_id=? ORDER BY step_number", (cid,)
        ).fetchall()
        for i, s in enumerate(steps, 1):
            conn.execute("UPDATE campaign_steps SET step_number=? WHERE id=?", (i, s["id"]))
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/leads/upload")
async def upload_leads(cid: int, file: UploadFile = File(...)):
    contents = await file.read()
    text = contents.decode("utf-8-sig", errors="ignore")  # handle BOM
    reader = csv.DictReader(io.StringIO(text))

    imported, skipped = 0, 0
    for row in reader:
        # Normalise keys
        normalised = {k.strip().lower().replace(" ", "_"): v.strip() for k, v in row.items()}

        email_val = None
        lead_data = {"first_name": "", "last_name": "", "company": "", "extra_data": {}}

        for raw_key, value in normalised.items():
            mapped = CSV_MAP.get(raw_key) or CSV_MAP.get(raw_key.replace("_", " "))
            if mapped == "email":
                email_val = value.lower()
            elif mapped in lead_data:
                lead_data[mapped] = value
            else:
                lead_data["extra_data"][raw_key] = value

        if not email_val or "@" not in email_val:
            skipped += 1
            continue

        with db() as conn:
            existing = conn.execute(
                "SELECT id FROM leads WHERE campaign_id=? AND email=?", (cid, email_val)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO leads (campaign_id, email, first_name, last_name, company, extra_data, next_send_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                (
                    cid,
                    email_val,
                    lead_data["first_name"],
                    lead_data["last_name"],
                    lead_data["company"],
                    json.dumps(lead_data["extra_data"]),
                ),
            )
            imported += 1

    return RedirectResponse(f"/sendy/campaigns/{cid}?imported={imported}&skipped={skipped}", status_code=303)


@app.post("/sendy/campaigns/{cid}/leads/{lid}/delete")
async def delete_lead(cid: int, lid: int):
    with db() as conn:
        conn.execute("DELETE FROM leads WHERE id=? AND campaign_id=?", (lid, cid))
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/start")
async def start_campaign(cid: int):
    with db() as conn:
        conn.execute("UPDATE campaigns SET status='active' WHERE id=?", (cid,))
        # Set next_send_at for any pending leads that don't have one
        conn.execute(
            "UPDATE leads SET next_send_at=datetime('now') WHERE campaign_id=? AND status='pending' AND next_send_at IS NULL",
            (cid,),
        )
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/pause")
async def pause_campaign(cid: int):
    with db() as conn:
        conn.execute("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
    return RedirectResponse(f"/sendy/campaigns/{cid}", status_code=303)


@app.post("/sendy/campaigns/{cid}/delete")
async def delete_campaign(cid: int):
    with db() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    return RedirectResponse("/sendy", status_code=303)
