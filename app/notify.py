"""Sends the delivery OTP to the customer.

By default the message is printed in the server console (a simulated SMS) and shown on the customer's screen.
Set SMS_WEBHOOK_URL to forward it to your own SMS gateway.
"""
import logging

import httpx

from . import config

log = logging.getLogger("notify")


def send_sms(phone, message):
    log.info("SMS to %s: %s", phone, message)
    if config.SMS_WEBHOOK_URL:
        try:
            httpx.post(config.SMS_WEBHOOK_URL, json={"to": phone, "message": message}, timeout=5)
        except Exception as e:
            log.warning("SMS webhook failed (%s)", e.__class__.__name__)

