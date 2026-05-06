import random
import logging
import datetime
from datetime import timezone
from apscheduler.schedulers.background import BackgroundScheduler
from database import db
from mailer import send_warm_email

logger = logging.getLogger(__name__)

# Warm-up ramp schedule: (start_day, end_day, emails_per_day)
RAMP = [
    (1,  7,   5),
    (8,  14,  10),
    (15, 21,  20),
    (22, 28,  35),
    (29, 35,  50),
    (36, 42,  75),
    (43, 999, 100),
]


# ── Warm-up ───────────────────────────────────────────────────────────────────

def emails_for_day(day: int) -> int:
    for start, end, count in RAMP:
        if start <= day <= end:
            return count
    return 100


def get_warm_day() -> int | None:
    with db() as conn:
        row = conn.execute("SELECT started_at FROM warm_start WHERE id = 1").fetchone()
        if not row:
            return None
        started = datetime.datetime.fromisoformat(row["started_at"])
        delta = datetime.datetime.now(timezone.utc) - started.replace(tzinfo=timezone.utc)
        return max(1, delta.days + 1)


def sent_today() -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM sent_emails WHERE date(sent_at) = date('now')"
        ).fetchone()
        return row["c"] if row else 0


def run_warm_job():
    day = get_warm_day()
    if day is None:
        return

    target = emails_for_day(day)
    already_sent = sent_today()
    to_send = max(0, target - already_sent)

    if to_send == 0:
        return

    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()
        partners = conn.execute("SELECT email FROM partners WHERE active = 1").fetchall()

    if not seed or not partners:
        return

    seed = dict(seed)
    partner_emails = [p["email"] for p in partners]
    targets = (partner_emails * ((to_send // len(partner_emails)) + 1))[:to_send]
    random.shuffle(targets)

    sent = 0
    for to_email in targets:
        try:
            subject = send_warm_email(seed, to_email)
            with db() as conn:
                conn.execute(
                    "INSERT INTO sent_emails (to_email, subject, day_number) VALUES (?, ?, ?)",
                    (to_email, subject, day),
                )
            sent += 1
        except Exception as e:
            logger.error(f"Warm-up send failed to {to_email}: {e}")

    logger.info(f"Warm-up day {day}: sent {sent}/{to_send}")


def send_now(count: int = 1) -> dict:
    day = get_warm_day()
    if day is None:
        return {"sent": 0, "error": "Warm-up not started."}

    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()
        partners = conn.execute("SELECT email FROM partners WHERE active = 1").fetchall()

    if not seed:
        return {"sent": 0, "error": "No seed email configured."}
    if not partners:
        return {"sent": 0, "error": "No active partner emails."}

    seed = dict(seed)
    partner_emails = [p["email"] for p in partners]
    pool = partner_emails * ((count // len(partner_emails)) + 2)
    targets = random.sample(pool, min(count, len(pool)))

    sent, errors = 0, []
    for to_email in targets:
        try:
            subject = send_warm_email(seed, to_email)
            with db() as conn:
                conn.execute(
                    "INSERT INTO sent_emails (to_email, subject, day_number) VALUES (?, ?, ?)",
                    (to_email, subject, day),
                )
            sent += 1
        except Exception as e:
            errors.append(str(e))

    return {"sent": sent, "errors": errors}


# ── Sendy ─────────────────────────────────────────────────────────────────────

def run_sendy_job():
    """Send any campaign emails that are due."""
    from sendy_mailer import send_campaign_email

    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()
        settings = conn.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()

    if not seed:
        return

    seed = dict(seed)
    tracking_url = (dict(settings).get("tracking_base_url") or "").strip() if settings else ""

    with db() as conn:
        due = conn.execute("""
            SELECT l.*, c.daily_limit, c.status AS campaign_status
            FROM leads l
            JOIN campaigns c ON l.campaign_id = c.id
            WHERE l.status IN ('pending', 'active')
              AND (l.next_send_at IS NULL OR l.next_send_at <= datetime('now'))
              AND c.status = 'active'
        """).fetchall()

    if not due:
        return

    # Group by campaign and respect daily limits
    from collections import defaultdict
    by_campaign: dict[int, list] = defaultdict(list)
    for row in due:
        by_campaign[row["campaign_id"]].append(dict(row))

    for campaign_id, leads in by_campaign.items():
        with db() as conn:
            sent_today_count = conn.execute("""
                SELECT COUNT(*) AS c FROM lead_events
                WHERE campaign_id = ? AND event_type = 'sent'
                AND date(created_at) = date('now')
            """, (campaign_id,)).fetchone()["c"]

        daily_limit = leads[0]["daily_limit"]
        remaining = daily_limit - sent_today_count
        if remaining <= 0:
            continue

        random.shuffle(leads)
        for lead in leads[:remaining]:
            try:
                next_step_num = lead["current_step"] + 1

                with db() as conn:
                    step = conn.execute(
                        "SELECT * FROM campaign_steps WHERE campaign_id=? AND step_number=?",
                        (campaign_id, next_step_num),
                    ).fetchone()

                if not step:
                    with db() as conn:
                        conn.execute("UPDATE leads SET status='completed' WHERE id=?", (lead["id"],))
                    continue

                step = dict(step)
                send_campaign_email(seed, lead, step, tracking_url or None)

                # Calculate next send time
                with db() as conn:
                    next_step = conn.execute(
                        "SELECT * FROM campaign_steps WHERE campaign_id=? AND step_number=?",
                        (campaign_id, next_step_num + 1),
                    ).fetchone()

                    if next_step:
                        delay = next_step["delay_days"] or 1
                        next_send = (
                            datetime.datetime.now(timezone.utc)
                            + datetime.timedelta(days=delay)
                        ).isoformat()
                        conn.execute(
                            "UPDATE leads SET status='active', current_step=?, next_send_at=? WHERE id=?",
                            (next_step_num, next_send, lead["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE leads SET status='completed', current_step=? WHERE id=?",
                            (next_step_num, lead["id"]),
                        )

                    conn.execute(
                        "INSERT INTO lead_events (lead_id, campaign_id, event_type, step_number) VALUES (?,?,?,?)",
                        (lead["id"], campaign_id, "sent", next_step_num),
                    )

            except Exception as e:
                logger.error(f"Sendy send failed to {lead['email']}: {e}")


def run_imap_job():
    """Poll inbox for replies and unsubscribes."""
    from imap_monitor import poll_inbox

    with db() as conn:
        seed = conn.execute("SELECT * FROM seed_email LIMIT 1").fetchone()

    if not seed:
        return

    result = poll_inbox(dict(seed))
    if result.get("skipped"):
        return

    logger.info(
        f"IMAP poll: replied={result['replied']} unsub={result['unsubscribed']} bounced={result['bounced']}"
    )


# ── Scheduler setup ───────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()


def start_scheduler():
    scheduler.add_job(run_warm_job,  "cron", hour=9,       minute=0,    id="warm_job",  replace_existing=True)
    scheduler.add_job(run_sendy_job, "cron", hour="8-17",  minute="*/30", id="sendy_job", replace_existing=True)
    scheduler.add_job(run_imap_job,  "cron", hour="7-19",  minute="*/30", id="imap_job",  replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (warm + sendy + imap).")


def stop_scheduler():
    scheduler.shutdown()
