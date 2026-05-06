import imaplib
import email
import re
import logging
from database import db

logger = logging.getLogger(__name__)

UNSUBSCRIBE_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bremove me\b",
    r"\bstop emailing\b",
    r"\bstop emails\b",
    r"\bopt.?out\b",
    r"\bplease remove\b",
    r"\btake me off\b",
    r"\bdon'?t (contact|email|message) me\b",
    r"\bdo not (contact|email|message) me\b",
    r"\bnot interested\b",
    r"\bno longer interested\b",
    r"\bleave me alone\b",
    r"\bremove (me )?from (your )?(list|mailing)\b",
]

BOUNCE_PATTERNS = [
    r"mailer.daemon",
    r"postmaster@",
    r"delivery.*failed",
    r"undelivered mail",
    r"returned mail",
    r"mail delivery (failure|notification)",
    r"address not found",
    r"user unknown",
    r"no such user",
    r"account does not exist",
]


def _check_unsubscribe(text: str) -> bool:
    text = text.lower()
    return any(re.search(p, text) for p in UNSUBSCRIBE_PATTERNS)


def _check_bounce(from_addr: str, subject: str) -> bool:
    combined = f"{from_addr} {subject}".lower()
    return any(re.search(p, combined) for p in BOUNCE_PATTERNS)


def _get_text(msg) -> str:
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            text = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return text


def _extract_email(header: str) -> str:
    match = re.search(r"[\w.+\-]+@[\w.\-]+\.\w+", header or "")
    return match.group(0).lower() if match else ""


def poll_inbox(seed: dict) -> dict:
    """
    Poll the seed inbox for replies and bounces from leads.
    Returns a summary dict.
    """
    if not seed.get("imap_host"):
        return {"skipped": True, "reason": "No IMAP host configured"}

    summary = {"replied": 0, "unsubscribed": 0, "bounced": 0, "errors": []}

    try:
        mail = imaplib.IMAP4_SSL(seed["imap_host"], int(seed.get("imap_port") or 993))
        mail.login(seed["smtp_user"], seed["smtp_pass"])
        mail.select("INBOX")

        # Search unseen messages only
        _, data = mail.search(None, "UNSEEN")
        msg_nums = data[0].split() if data[0] else []

        if not msg_nums:
            mail.logout()
            return summary

        # Load active leads indexed by email
        with db() as conn:
            active_leads = conn.execute(
                "SELECT * FROM leads WHERE status NOT IN ('unsubscribed', 'bounced', 'completed')"
            ).fetchall()

        lead_map = {row["email"].lower(): dict(row) for row in active_leads}

        for num in msg_nums:
            try:
                _, raw = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])

                from_addr = _extract_email(msg.get("From", ""))
                subject = msg.get("Subject", "")
                body = _get_text(msg)

                # Bounce detection (from MAILER-DAEMON etc.)
                if _check_bounce(msg.get("From", ""), subject):
                    # Try to find bounced lead email in the body
                    for lead_email in lead_map:
                        if lead_email in body.lower():
                            lead = lead_map[lead_email]
                            with db() as conn:
                                conn.execute(
                                    "UPDATE leads SET status='bounced' WHERE id=?",
                                    (lead["id"],),
                                )
                                conn.execute(
                                    "INSERT INTO lead_events (lead_id, campaign_id, event_type, detail) VALUES (?,?,?,?)",
                                    (lead["id"], lead["campaign_id"], "bounced", subject[:200]),
                                )
                            summary["bounced"] += 1
                            logger.info(f"Bounced: {lead_email}")
                    continue

                if from_addr not in lead_map:
                    continue

                lead = lead_map[from_addr]
                is_unsub = _check_unsubscribe(body) or _check_unsubscribe(subject)

                with db() as conn:
                    if is_unsub:
                        conn.execute(
                            "UPDATE leads SET status='unsubscribed' WHERE id=?",
                            (lead["id"],),
                        )
                        conn.execute(
                            "INSERT INTO lead_events (lead_id, campaign_id, event_type, detail) VALUES (?,?,?,?)",
                            (lead["id"], lead["campaign_id"], "unsubscribed", "Opt-out detected"),
                        )
                        summary["unsubscribed"] += 1
                        logger.info(f"Unsubscribed: {from_addr}")
                    elif lead["status"] not in ("replied",):
                        conn.execute(
                            "UPDATE leads SET status='replied' WHERE id=?",
                            (lead["id"],),
                        )
                        conn.execute(
                            "INSERT INTO lead_events (lead_id, campaign_id, event_type, detail) VALUES (?,?,?,?)",
                            (lead["id"], lead["campaign_id"], "replied", subject[:200]),
                        )
                        summary["replied"] += 1
                        logger.info(f"Reply from: {from_addr}")

            except Exception as e:
                summary["errors"].append(str(e))
                logger.error(f"Error processing message {num}: {e}")

        mail.logout()

    except Exception as e:
        summary["errors"].append(str(e))
        logger.error(f"IMAP poll error: {e}")

    return summary
