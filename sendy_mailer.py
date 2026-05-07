import os
import secrets
import json
import logging
import resend
from database import db

logger = logging.getLogger(__name__)

resend.api_key = os.environ.get("RESEND_API_KEY", "")


def personalize(text: str, lead: dict) -> str:
    replacements = {
        "first_name": lead.get("first_name") or "",
        "last_name": lead.get("last_name") or "",
        "company": lead.get("company") or "",
        "email": lead.get("email") or "",
        "full_name": f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip(),
    }
    try:
        extra = json.loads(lead.get("extra_data") or "{}")
        replacements.update({k: str(v) for k, v in extra.items()})
    except Exception:
        pass

    for key, value in replacements.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def send_campaign_email(seed: dict, lead: dict, step: dict, tracking_base_url: str = None) -> str:
    token = secrets.token_urlsafe(20) if tracking_base_url else ""

    subject = personalize(step["subject"], lead)
    plain_body = personalize(step["body"], lead)

    params = {
        "from": f"{seed['name']} <{seed['email']}>",
        "to": [lead["email"]],
        "subject": subject,
        "text": plain_body,
    }

    if tracking_base_url and token:
        pixel_url = f"{tracking_base_url.rstrip('/')}/t/{token}"
        html_body = plain_body.replace("\n", "<br>")
        params["html"] = (
            f"<html><body>{html_body}"
            f'<img src="{pixel_url}" width="1" height="1" style="display:none;border:0;" />'
            f"</body></html>"
        )

    resend.Emails.send(params)

    if token:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO open_tokens (token, lead_id, campaign_id, step_number) VALUES (?, ?, ?, ?)",
                (token, lead["id"], lead["campaign_id"], step["step_number"]),
            )

    logger.info(f"Campaign email sent to {lead['email']} (step {step['step_number']})")
    return token
