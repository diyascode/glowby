"""Tests for v0.13.2 side-detail cap in output.py build_report."""
import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.agents.output import build_report


def mk(claim, ts, central=True, risk="low", state="supported"):
    return {
        "claim": claim, "gate_label": "factual", "central": central,
        "risk_level": risk, "public_safety_risk": False,
        "verdict": {"truth_score": ts, "verdict_state": state,
                    "verdict": "test.", "evidence_strength": "strong",
                    "key_sources": []},
    }


def report(claims, title="t"):
    return build_report({"title": title, "claims": claims})["report"]


fails = []

# 1. THE CIVICS CASE: central 9.5, side detail 7.5 -> headline 7.5, capped
r = report([mk("100 senators", 9.5), mk("100 questions", 7.5, central=False)])
if r["headline_score"] != 7.5: fails.append(f"1 score {r['headline_score']}")
if r["headline_state"] != "mostly_accurate": fails.append(f"1 state {r['headline_state']}")
if "capped" not in r["headline_label"]: fails.append(f"1 label {r['headline_label']}")

# 2. EARLIER CIVICS CASE: central 9.2, side 6.5 -> floor holds at 7.5 (not 6.5 mixed!)
r = report([mk("central", 9.2), mk("side", 6.5, central=False)])
if r["headline_score"] != 7.5: fails.append(f"2 score {r['headline_score']}")
if r["headline_state"] != "mostly_accurate": fails.append(f"2 state {r['headline_state']}")
if "capped" not in r["headline_label"]: fails.append(f"2 label {r['headline_label']}")

# 3. Accurate side (9.0 >= 8.0 band) leaves headline ALONE -> 9.5
r = report([mk("central", 9.5), mk("side", 9.0, central=False)])
if r["headline_score"] != 9.5: fails.append(f"3 score {r['headline_score']}")
if "capped" in r["headline_label"]: fails.append("3 capped wrongly")
if "side detail scored lower" not in r["headline_label"]: fails.append("3 missing disclosure")

# 3b. Side at 7.9 (below accurate band) -> headline 7.9, capped language
r = report([mk("central", 9.5), mk("side", 7.9, central=False)])
if r["headline_score"] != 7.9: fails.append(f"3b score {r['headline_score']}")
if "capped" not in r["headline_label"]: fails.append("3b missing cap language")

# 4. Anti-smuggling backstop: HIGH-RISK side claim counts at FULL weight
r = report([mk("central", 9.5), mk("dangerous aside", 2.0, central=False, risk="high", state="contradicted")])
if r["headline_score"] != 2.0: fails.append(f"4 score {r['headline_score']}")
if r["headline_state"] != "misleading": fails.append(f"4 state {r['headline_state']}")

# 5. Central claim itself is wrong -> full weight, NO floor rescue
r = report([mk("central lie", 2.0, state="contradicted"), mk("side", 9.0, central=False)])
if r["headline_score"] != 2.0: fails.append(f"5 score {r['headline_score']}")

# 6. NO central claims scorable -> all sides count fully (no floor abuse)
r = report([mk("side a", 3.0, central=False, state="contradicted"),
            mk("side b", 4.0, central=False)])
if r["headline_score"] != 3.0: fails.append(f"6 score {r['headline_score']}")

# 7. Single central claim only -> untouched
r = report([mk("solo", 8.8)])
if r["headline_score"] != 8.8: fails.append(f"7 score {r['headline_score']}")
if "capped" in r["headline_label"] or "lower" in r["headline_label"]:
    fails.append(f"7 label {r['headline_label']}")

# 8. Side exactly at floor with central at floor too -> 7.5, no cap language
r = report([mk("central", 7.5), mk("side", 7.5, central=False)])
if r["headline_score"] != 7.5: fails.append(f"8 score {r['headline_score']}")
if "capped" in r["headline_label"]: fails.append("8 capped wrongly")

print("FAILURES:", fails) if fails else print("ALL 8 PASS: side-detail cap, floor, backstop, no-central fallback")
