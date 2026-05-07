import os
import random
import smtplib
import logging
import resend

logger = logging.getLogger(__name__)

resend.api_key = os.environ.get("RESEND_API_KEY", "")

SUBJECTS = [
    "Quick question for you",
    "Following up",
    "Checking in",
    "Thoughts on this?",
    "Had a chance to look at this?",
    "Quick update",
    "Worth a conversation?",
    "A few things to share",
    "Let me know what you think",
    "Wanted to reach out",
    "Just circling back",
    "Free for a quick chat?",
    "Something I wanted to share",
    "Your thoughts?",
    "Touching base",
    "Quick note",
    "Hope this finds you well",
    "Been meaning to reach out",
    "A thought I wanted to run by you",
    "Catching up",
]

BODIES = [
    """\
Hi,

Hope you're having a good week. I wanted to reach out and see how things are going on your end.

Let me know if you'd like to connect soon — happy to find a time that works.

Best,
{name}""",

    """\
Hey,

Just wanted to drop a quick note. Things have been busy here but I've been thinking about our last conversation.

Would love to get your thoughts when you have a moment.

Talk soon,
{name}""",

    """\
Hi there,

Hope all is well. I've been meaning to reach out for a while now — just wanted to say hi and see how things are going.

Feel free to reply whenever convenient.

Best,
{name}""",

    """\
Hey,

Quick one — do you have a few minutes this week to catch up? Nothing urgent, just wanted to touch base.

Let me know what works for you.

Cheers,
{name}""",

    """\
Hi,

I came across something the other day that made me think of you. Happy to share more details if you're interested.

Hope things are going well on your end.

Best,
{name}""",

    """\
Hey,

Just circling back on something we discussed a while ago. I think there might be an opportunity worth exploring together.

Would love to get your take on it.

Talk soon,
{name}""",

    """\
Hi,

Hope this finds you well. I've been heads-down lately but wanted to check in and see how things are going.

Let me know if you'd like to reconnect.

Best,
{name}""",

    """\
Hey,

Had a few thoughts I wanted to run by you. Nothing major, just something I think could be useful.

Let me know if you have a moment to chat.

Cheers,
{name}""",

    """\
Hi,

Just a quick note to say hi. It's been a while and I thought it'd be good to check in.

Hope everything's going well your end.

Best,
{name}""",

    """\
Hey,

I wanted to follow up on a few things. When you get a chance, it'd be great to connect.

No rush — just whenever works for you.

Talk soon,
{name}""",
]


def send_warm_email(seed: dict, to_email: str) -> str:
    subject = random.choice(SUBJECTS)
    body = random.choice(BODIES).format(name=seed["name"].split()[0])

    resend.Emails.send({
        "from": f"{seed['name']} <{seed['email']}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
    })

    return subject


def test_connection(seed: dict) -> dict:
    """Test IMAP connectivity (used for reply monitoring)."""
    if not seed.get("imap_host"):
        return {"ok": False, "error": "No IMAP host set — add imap.zoho.com.au to enable reply monitoring."}
    try:
        with smtplib.SMTP_SSL(seed["imap_host"].replace("imap.", "smtp."), 465, timeout=10) as s:
            pass
    except Exception:
        pass  # IMAP test below is the real check

    import imaplib
    try:
        mail = imaplib.IMAP4_SSL(seed["imap_host"], int(seed.get("imap_port") or 993), timeout=10)
        mail.login(seed["smtp_user"], seed["smtp_pass"])
        mail.logout()
        return {"ok": True}
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": f"IMAP auth failed — check your app password. ({e})"}
    except OSError as e:
        return {"ok": False, "error": f"Could not connect to {seed['imap_host']}:993 — {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
