#!/usr/bin/env python3
"""
One-off script to analyze support_conversations and support_ai.log.
Output: JSON and text stats for AI_SUPPORT_ANALYTICS_REPORT.md.
Do not modify app code.
"""
import os
import sys
import json
import re
from collections import defaultdict
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    out = {"db_available": False, "log_available": False, "db_rows": [], "log_lines": []}

    # 1. Try DB
    try:
        from app.db import get_conn
        import psycopg2.extras
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT id, telegram_user_id, user_message, ai_response,
                           detected_intent, confidence, mode, handoff_to_human, created_at
                    FROM support_conversations
                    ORDER BY created_at ASC
                """)
                rows = cur.fetchall()
                out["db_available"] = True
                out["db_rows"] = [
                    {
                        "id": r["id"],
                        "telegram_user_id": r["telegram_user_id"],
                        "user_message": (r["user_message"] or "")[:200],
                        "ai_response": (r["ai_response"] or "")[:200] if r["ai_response"] else None,
                        "detected_intent": r["detected_intent"],
                        "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                        "mode": r["mode"],
                        "handoff_to_human": bool(r["handoff_to_human"]),
                        "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
                    }
                    for r in rows
                ]
    except Exception as e:
        out["db_error"] = str(e)

    # 2. Try log file (support_ai)
    for log_path in ["logs/support_ai.log", "/app/logs/support_ai.log"]:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), log_path) if not os.path.isabs(log_path) else log_path
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                out["log_available"] = True
                out["log_path"] = path
                # Parse: support_ai tg_id=... intent=... conf=... action=... fallback=... handoff=... resend=... vpn_diagnosis=...
                pat = re.compile(
                    r"support_ai tg_id=(\d+) intent=(\S+) conf=([\d.]+) action=(\S+) fallback=(\S+) handoff=(\S+) resend=(\S+) vpn_diagnosis=(\S*)"
                )
                for line in lines:
                    m = pat.search(line)
                    if m:
                        out["log_lines"].append({
                            "tg_id": int(m.group(1)),
                            "intent": m.group(2),
                            "conf": float(m.group(3)),
                            "action": m.group(4),
                            "fallback": m.group(5) == "True",
                            "handoff": m.group(6) == "True",
                            "resend": m.group(7) == "True",
                            "vpn_diagnosis": m.group(8) if m.group(8) else None,
                        })
            except Exception as e:
                out["log_error"] = str(e)
            break

    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
