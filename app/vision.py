"""Optional local vision check for whether a product photo matches its listing text.

This is a moderation aid for an admin. It cannot verify a seller's identity, prove authenticity,
or automatically publish, hide, or block anything.
"""
import base64
import json
import re

import httpx

from . import config


class VisionUnavailable(RuntimeError):
    pass


def status():
    try:
        response = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        response.raise_for_status()
        names = {m.get("name", "") for m in response.json().get("models", [])}
        installed = config.VISION_MODEL in names
        return {"available": installed, "model": config.VISION_MODEL,
                "setup": "ollama pull " + config.VISION_MODEL}
    except Exception:
        return {"available": False, "model": config.VISION_MODEL,
                "setup": "Start Ollama, then run: ollama pull " + config.VISION_MODEL}


def compare_product_image(product_name, description, image_path):
    state = status()
    if not state["available"]:
        raise VisionUnavailable(state["setup"])
    try:
        image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError:
        raise VisionUnavailable("The product image file could not be read.")
    prompt = (
        "Compare this product listing text with the visible contents of the supplied product photo. "
        "Assess only whether the image appears visually consistent with the listed product. "
        "Do not identify, judge, or make claims about any person in the image. This cannot prove origin, "
        "freshness, ownership, or seller identity. Return JSON only with keys verdict and reason. "
        "verdict must be consistent, possible_mismatch, or uncertain. Use possible_mismatch only when "
        "the visible item clearly differs from the named product; use uncertain for unclear, packaged, "
        "or mixed images. Keep reason short and describe visible evidence."
    )
    response = httpx.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={"model": config.VISION_MODEL, "stream": False, "format": "json",
              "options": {"temperature": 0},
              "messages": [{"role": "user", "content": f"Product name: {product_name}\nDescription: {description or '(none)'}\n{prompt}",
                            "images": [image]}]},
        timeout=120,
    )
    response.raise_for_status()
    raw = response.json().get("message", {}).get("content", "")
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            result = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            result = {}
    verdict = result.get("verdict")
    if verdict not in {"consistent", "possible_mismatch", "uncertain"}:
        verdict = "uncertain"
    reason = str(result.get("reason") or "The model did not return a clear reason.")[:500]
    return {"verdict": verdict, "reason": reason, "model": config.VISION_MODEL,
            "human_review_required": True, "automatic_action": "none"}

