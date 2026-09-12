"""
Detector calibration — measure, don't guess.

Glowby's thresholds (0.90 likely, 0.50 unclear, 0.10 weak) came from the
vendor's docs, not from our own videos. This tool runs a labelled set —
lines of "ai <url>" / "real <url>" — through the SAME lane readers get
(Stage 1 labels, Hive frames + adaptive second pass, audio, forensic
second opinion; no reverse search, nothing stored as a result) and
reports, per item, what the lane concluded, and overall: the detection
rate on known-AI, the false-alarm rate on known-real, and what those
rates WOULD be at other thresholds — so a threshold change is a decision
with numbers behind it. Runs in the background; budget-guarded; about
4-5 cents per item.
"""

import re
import time

from app.agents import hive_detect
from app.agents.authenticity import assess_stage1
from app.agents.detection import run_media_detection
from app.agents.ingest import ingest, IngestError

MAX_ITEMS = 40
_LINE = re.compile(r"^\s*(ai|real|fake|synthetic|genuine)\s*[:,\-\s]\s*(https?://\S+)\s*$", re.I)


def parse_items(text: str) -> list:
    """Pure (unit-tested): 'ai <url>' / 'real <url>' lines -> items."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        lab = m.group(1).lower()
        lab = "ai" if lab in ("ai", "fake", "synthetic") else "real"
        out.append({"label": lab, "url": m.group(2)})
        if len(out) >= MAX_ITEMS:
            break
    return out


def summarize(rows: list) -> dict:
    """Pure (unit-tested): rates at the live thresholds and at alternatives.
    A row counts as 'flagged' at threshold T when its top_score >= T OR a
    declared/verified label exists (labels are not threshold-dependent)."""
    ai = [r for r in rows if r.get("label") == "ai" and r.get("ok")]
    real = [r for r in rows if r.get("label") == "real" and r.get("ok")]

    def flagged(r, t):
        if r.get("origin") in ("declared_ai", "verified_ai_provenance"):
            return True
        ts = r.get("top_score")
        return ts is not None and float(ts) >= t

    out = {"ai_items": len(ai), "real_items": len(real),
           "failed": sum(1 for r in rows if not r.get("ok")), "at": {}}
    for t in (0.50, 0.70, 0.90):
        det = sum(1 for r in ai if flagged(r, t))
        fa = sum(1 for r in real if flagged(r, t))
        out["at"][str(t)] = {"detected": det, "detection_rate": (round(det / len(ai), 2) if ai else None),
                             "false_alarms": fa, "false_alarm_rate": (round(fa / len(real), 2) if real else None)}
    # the lane's actual verdict (origin), independent of thresholds
    lane_hit = sum(1 for r in ai if r.get("origin") in ("likely_synthetic", "declared_ai", "verified_ai_provenance"))
    lane_unclear_ai = sum(1 for r in ai if r.get("origin") == "inconclusive")
    lane_fa = sum(1 for r in real if r.get("origin") in ("likely_synthetic",))
    lane_unclear_real = sum(1 for r in real if r.get("origin") == "inconclusive")
    out["lane"] = {"ai_caught": lane_hit, "ai_unclear": lane_unclear_ai, "ai_missed": len(ai) - lane_hit - lane_unclear_ai,
                   "real_false_alarm": lane_fa, "real_unclear": lane_unclear_real,
                   "real_clean": len(real) - lane_fa - lane_unclear_real}
    op_ai = [r.get("opinion") for r in ai if r.get("opinion")]
    op_real = [r.get("opinion") for r in real if r.get("opinion")]
    out["opinion"] = {"ai_high": op_ai.count("high"), "ai_medium": op_ai.count("medium"), "ai_low": op_ai.count("low"),
                      "real_high": op_real.count("high"), "real_medium": op_real.count("medium"), "real_low": op_real.count("low")}
    return out


def run_calibration(items: list, progress=None) -> dict:
    t0 = time.time()
    rows = []
    for i, it in enumerate(items[:MAX_ITEMS]):
        row = {"url": it["url"], "label": it["label"], "ok": False}
        try:
            res = ingest(it["url"])
            frames = res.pop("frames", None) or res.pop("frames_media", None) or []
            audio = res.pop("audio_clip_b64", None)
            from app.agents.ingest import take_extras
            audio = audio or take_extras().get("audio_clip_b64")
            tr = res.get("transcript") or ""
            vis = tr.split("[WHAT THE VIDEO VISUALLY SHOWS]", 1)[1] if "[WHAT THE VIDEO VISUALLY SHOWS]" in tr else ""
            au = assess_stage1(caption=res.get("title") or "", ocr_text=vis, transcript=tr,
                               platform_label=res.get("platform_ai_label"))
            au = run_media_detection(au, frames=frames[:6], extra_frames=frames[6:12], audio_b64=audio,
                                     reason="calibration", allow_reverse=False,
                                     person_hint=(res.get("title") or "") + " " + tr[:300])
            op = next((e for e in (au.get("evidence") or []) if e.get("provider") == "claude_vision"), None)
            aud = next((e for e in (au.get("evidence") or []) if e.get("signal_type") == "forensic_audio_voice"), None)
            row.update({"ok": True, "title": (res.get("title") or "")[:80], "platform": res.get("platform"),
                        "origin": au.get("origin_result"), "top_score": au.get("top_score"),
                        "second_pass": bool(au.get("second_pass")),
                        "opinion": (op or {}).get("likelihood"), "audio_score": (aud or {}).get("raw_score"),
                        "declared": any(e.get("provider") == "labels" for e in (au.get("evidence") or [])),
                        "frames": au.get("frames_analyzed")})
        except IngestError as e:
            row["error"] = str(e)[:160]
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        rows.append(row)
        if progress:
            try:
                progress(i + 1, len(items[:MAX_ITEMS]))
            except Exception:
                pass
    doc = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "rows": rows, "seconds": round(time.time() - t0, 1),
           "thresholds": {"likely": hive_detect.THRESH_LIKELY, "inconclusive": hive_detect.THRESH_INCONCLUSIVE,
                          "weak": hive_detect.THRESH_WEAK},
           "est_cost": round(0.045 * sum(1 for r in rows if r.get("ok")), 2)}
    doc["summary"] = summarize(rows)
    return doc
