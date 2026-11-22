# -*- coding: utf-8 -*-
"""信箱 v1：两台机之间离线互传的三个接口，端口 8790。

POST /mail/send     投信（要 X-Mail-Key，或 ?key=）
GET  /mail/inbox    取信（since / to）
GET  /mail/health   活着吗
GET  /dl/<name>     附件下载（免口令，只读 <数据目录>/dl）

存储：<数据目录>/mail.jsonl，一行一封信，UTF-8。
"""

import json
import logging
import logging.handlers
import os
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

HERE = Path(__file__).resolve().parent
MAIL_DIR = HERE / "mail"
DL_DIR = MAIL_DIR / "dl"
LOG = MAIL_DIR / "mail.jsonl"
KEY = os.environ.get("MAIL_KEY", "")   # 空 = 拒绝所有写操作（默认安全）
ME = os.environ.get("MAIL_ME", "电脑")
PORT = int(os.environ.get("MAIL_PORT", "8790"))

MAIL_DIR.mkdir(parents=True, exist_ok=True)
DL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(
                  str(HERE / "mailbox.log"), maxBytes=10 * 1024 * 1024,
                  backupCount=3, encoding="utf-8")],
)
log = logging.getLogger("mail")

app = FastAPI()


def authed(req: Request) -> bool:
    k = req.headers.get("X-Mail-Key") or req.query_params.get("key") or ""
    return k == KEY


def load_rows():
    rows = []
    if LOG.exists():
        with open(LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


@app.post("/mail/send")
async def send(req: Request):
    if not authed(req):
        return JSONResponse({"ok": False, "err": "bad key"}, status_code=403)
    try:
        d = await req.json()
    except Exception:
        d = {}
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "from": str(d.get("from") or "?"),
        "to": str(d.get("to") or "*"),
        # 只认 text。以前发信端写 body，结果静默存了一封空信（2026-11-20 踩过），
        # 所以这里显式拒绝空正文，别再把「发出去但没人收到内容」当成成功。
        "text": str(d.get("text") or d.get("body") or "")[:20000],
        "files": [str(x) for x in (d.get("files") or [])][:32],
    }
    if not rec["text"].strip() and not rec["files"]:
        log.warning("mail rejected: empty text from=%s to=%s", rec["from"], rec["to"])
        return JSONResponse({"ok": False, "err": "empty text"}, status_code=400)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.info("mail in from=%s to=%s len=%d files=%s", rec["from"], rec["to"],
             len(rec["text"]), rec["files"])
    return {"ok": True, "id": rec["id"], "ts": rec["ts"]}


@app.get("/mail/inbox")
def inbox(req: Request, since: float = 0.0, to: str = ""):
    if not authed(req):
        return JSONResponse({"ok": False, "err": "bad key"}, status_code=403)
    rows = [r for r in load_rows() if float(r.get("ts", 0)) > since]
    if to:
        rows = [r for r in rows if r.get("to") in (to, "*")]
    return {"ok": True, "me": ME, "now": time.time(), "msgs": rows[:500]}


@app.get("/mail/health")
def health():
    rows = load_rows()
    last = max((r.get("ts", 0) for r in rows), default=0)
    return {"ok": True, "me": ME, "key_set": bool(KEY), "count": len(rows),
            "last_ts": last, "dl": [p.name for p in DL_DIR.glob("*")][:50]}


@app.get("/dl/{name}")
def download(name: str):
    safe = os.path.basename(name)
    p = DL_DIR / safe
    if not p.is_file():
        return JSONResponse({"ok": False, "err": "not found"}, status_code=404)
    return FileResponse(str(p), filename=safe)


if __name__ == "__main__":
    log.info("mailbox starting on %d, me=%s, log=%s", PORT, ME, LOG)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None, access_log=False)
