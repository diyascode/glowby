"""
One door for every Claude call (v0.66.20, Sept 29 — the Sonnet 5.5 switch).

Why: the 5.x models reject a non-default temperature with a 400, and
Glowby asks every judge for temperature=0 (same claim + same evidence ->
same verdict). Switching the model by env var alone would have failed
every check. This wrapper drops `temperature` for the 5.x family up
front, and — belt and braces — retries ONCE without it when any model
answers "temperature" in a 400. Nothing else about the call changes.
"""

import re

# families that only accept the default sampling settings
_NO_TEMPERATURE = re.compile(r"^claude-(sonnet|opus|haiku)-(5|6|7)(\b|-|\.)", re.I)


def accepts_temperature(model: str) -> bool:
    """Pure: may this model be asked for temperature=0?"""
    return not _NO_TEMPERATURE.match(str(model or ""))


def create(client, **kw):
    """client.messages.create with the sampling parameters the model allows."""
    if not accepts_temperature(kw.get("model", "")):
        for k in ("temperature", "top_p", "top_k"):
            kw.pop(k, None)
    try:
        return client.messages.create(**kw)
    except Exception as e:
        msg = str(e).lower()
        if any(k in kw for k in ("temperature", "top_p", "top_k")) and (
                "temperature" in msg or "top_p" in msg or "top_k" in msg):
            for k in ("temperature", "top_p", "top_k"):
                kw.pop(k, None)
            return client.messages.create(**kw)
        raise
