# -*- coding: utf-8 -*-
"""
RM Delay — ห้องเย็น : server สำหรับเครื่องในบริษัท
- เสิร์ฟแอปมือถือ (static/index.html) + API
- เก็บข้อมูลใน SQLite, รูปหลักฐานเป็นไฟล์ .jpg
- /api/ocr อ่านข้อความบนสลิปด้วย PaddleOCR (ชื่อวัตถุดิบ แผนการผลิต เวลาเข้าห้องเย็นที่เขียนมือ ฯลฯ)

รัน:  python server.py            (ค่าเริ่มต้น https://0.0.0.0:8443 ถ้ามีไฟล์ cert/key)
ตั้งค่าด้วย environment variables (ดู CONFIG ด้านล่าง หรือ README_TH.md)
"""
import base64
import csv
import difflib
import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# ============================ CONFIG ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RM_DELAY_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "rm_delay.db")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
SLIP_DIR = os.path.join(DATA_DIR, "slips")          # รูปสลิปที่ส่งมาอ่าน OCR (เก็บไว้ดูย้อนหลัง/ปรับปรุง)
SAVE_SLIPS = os.environ.get("RM_DELAY_SAVE_SLIPS", "1") == "1"
ADMIN_PIN = os.environ.get("RM_DELAY_ADMIN_PIN", "0000")   # PIN สำหรับลบรายการ — เปลี่ยนก่อนใช้งานจริง
OCR_LANG = os.environ.get("RM_DELAY_OCR_LANG", "th")      # th อ่านได้ทั้งไทยและอังกฤษ/ตัวเลข
HOST = os.environ.get("RM_DELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("RM_DELAY_PORT", "8443"))
SSL_CERT = os.environ.get("RM_DELAY_CERT", os.path.join(BASE_DIR, "cert", "cert.pem"))
SSL_KEY = os.environ.get("RM_DELAY_KEY", os.path.join(BASE_DIR, "cert", "key.pem"))

os.makedirs(PHOTO_DIR, exist_ok=True)
os.makedirs(SLIP_DIR, exist_ok=True)

# ============================ DATABASE ============================
_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS delays(
              id TEXT PRIMARY KEY,
              ts TEXT NOT NULL,          -- ISO เวลาบันทึก
              date TEXT NOT NULL,        -- วันผลิต YYYY-MM-DD (NS หลังเที่ยงคืน = วันก่อน)
              shift TEXT,
              delay INTEGER DEFAULT 1,
              data TEXT NOT NULL,        -- ข้อมูลทั้งหมด (JSON)
              created TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_delays_date ON delays(date);
            CREATE INDEX IF NOT EXISTS ix_delays_ts ON delays(ts);
            CREATE TABLE IF NOT EXISTS materials(
              code TEXT PRIMARY KEY, name TEXT, wi TEXT, updated TEXT
            );
            CREATE TABLE IF NOT EXISTS config(
              key TEXT PRIMARY KEY, value TEXT
            );
            """
        )


init_db()

SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,120}$")


def row_to_rec(r: sqlite3.Row) -> Dict[str, Any]:
    d = json.loads(r["data"])
    d["id"] = r["id"]
    return d


# ============================ OCR ============================
_ocr = None
_ocr_ver = 0
_ocr_err = ""
_ocr_lock = threading.Lock()


def _init_ocr():
    """โหลด PaddleOCR (รองรับทั้งเวอร์ชัน 2.x และ 3.x) — ปิด mkldnn กันบั๊ก oneDNN บน CPU"""
    global _ocr, _ocr_ver, _ocr_err
    try:
        from paddleocr import PaddleOCR  # noqa
    except Exception as e:  # pragma: no cover
        _ocr_err = f"ยังไม่ได้ติดตั้ง paddleocr: {e}"
        return
    for lang in [OCR_LANG] + (["en"] if OCR_LANG != "en" else []):
        if _try_ocr(PaddleOCR, lang):
            if lang != OCR_LANG:
                print(f"! โหลดภาษา {OCR_LANG} ไม่ได้ ใช้ {lang} แทน (อ่านหัวข้อภาษาไทยไม่ได้)")
            return


def _try_ocr(PaddleOCR, lang) -> bool:
    global _ocr, _ocr_ver, _ocr_err
    try:
        _ocr = PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            enable_mkldnn=False,
        )
        _ocr_ver = 3
        _ocr_err = ""
        return True
    except TypeError:
        try:
            _ocr = PaddleOCR(lang=lang, use_angle_cls=True, enable_mkldnn=False, show_log=False)
            _ocr_ver = 2
            _ocr_err = ""
            return True
        except Exception as e:
            _ocr_err = f"โหลด PaddleOCR ไม่ได้: {e}"
    except Exception as e:
        _ocr_err = f"โหลด PaddleOCR ไม่ได้: {e}"
    return False


def _warm():
    with _ocr_lock:
        if _ocr is None and not _ocr_err:
            _init_ocr()


threading.Thread(target=_warm, daemon=True).start()


def _run_ocr(img) -> List[Dict[str, Any]]:
    """คืนรายการกล่องข้อความ [{text, score, x, y, h}] (x,y = จุดกึ่งกลาง)"""
    boxes = []
    if _ocr_ver == 3:
        res = _ocr.predict(img)
        for r in res or []:
            d = r
            if not hasattr(d, "get"):
                d = getattr(r, "json", {}) or {}
                d = d.get("res", d)
            texts = list(d.get("rec_texts", []) or [])
            scores = list(d.get("rec_scores", []) or [])
            polys = d.get("rec_polys", None)
            if polys is None:
                polys = d.get("dt_polys", [])
            for i, t in enumerate(texts):
                p = polys[i] if i < len(polys) else None
                if p is None:
                    continue
                xs = [float(q[0]) for q in p]
                ys = [float(q[1]) for q in p]
                boxes.append({"text": str(t), "score": float(scores[i]) if i < len(scores) else 0.0,
                              "x": sum(xs) / len(xs), "y": sum(ys) / len(ys), "h": max(ys) - min(ys),
                              "x0": min(xs)})
    else:
        res = _ocr.ocr(img, cls=True)
        for page in res or []:
            for item in page or []:
                p, (t, sc) = item[0], item[1]
                xs = [float(q[0]) for q in p]
                ys = [float(q[1]) for q in p]
                boxes.append({"text": str(t), "score": float(sc), "x": sum(xs) / len(xs), "y": sum(ys) / len(ys),
                              "h": max(ys) - min(ys), "x0": min(xs)})
    return boxes


def group_lines(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """รวมกล่องที่อยู่แถวเดียวกันเป็นบรรทัด เรียงบน→ล่าง ซ้าย→ขวา"""
    if not boxes:
        return []
    hs = sorted(b["h"] for b in boxes if b["h"] > 0) or [20]
    med = hs[len(hs) // 2]
    lines: List[Dict[str, Any]] = []
    for b in sorted(boxes, key=lambda b: b["y"]):
        if lines and abs(b["y"] - lines[-1]["y"]) < med * 0.55:
            ln = lines[-1]
            ln["boxes"].append(b)
            ln["y"] = sum(x["y"] for x in ln["boxes"]) / len(ln["boxes"])
        else:
            lines.append({"y": b["y"], "boxes": [b]})
    out = []
    for ln in lines:
        bs = sorted(ln["boxes"], key=lambda b: b["x0"])
        out.append({"y": ln["y"], "text": " ".join(b["text"] for b in bs).strip(),
                    "score": min(b["score"] for b in bs)})
    return out


# ---------- แปลงข้อความ OCR เป็นช่องข้อมูล ----------
LABELS = {
    "shift": ["เตรียมงานให้กะ", "ให้กะ"],
    "item": ["รายการ"],
    "rm_name": ["ชื่อวัตถุดิบ"],
    "mat_code": ["รหัสวัตถุดิบ(mat)", "รหัสวัตถุดิบ"],
    "batch_tag": ["Batchจากป้ายTagห้องเย็นใหญ่", "Batchจากป้าย"],
    "batch_prep": ["Batchหลังเตรียม"],
    "plan": ["แผนการผลิต"],
    "level_eu": ["LevelEu(สำหรับปลา)", "LevelEu"],
    "process": ["ประเภทการแปรรูป"],
    "trays": ["จำนวนถาด"],
    "out_big_chill": ["วันที่/เวลาออกจากห้องเย็นใหญ่", "ออกจากห้องเย็นใหญ่"],
    "summary_date": ["วันที่สรุปเบิก"],
    "prep_done": ["วันที่/เวลาเตรียมเสร็จ/ผสมเสร็จ", "เตรียมเสร็จ"],
    "prep_point": ["จุดออกใบเสร็จ"],
    "operator": ["ผู้ดำเนินการ"],
    "temperature": ["Temperature"],
    "qc_by": ["ผู้ดำเนินการตรวจสอบ"],
    "cold_date": ["วันที่เข้าห้องเย็น"],
    "cold_time": ["เวลา"],
}
PLAN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{2,5}[A-Za-z]?)\s*[\(\[\{]\s*([^()\[\]{}\n]{1,30}?)\s*[\)\]\}]")
DT_RE = re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s+(\d{1,2})\s*[:.]\s*(\d{2})")


def _norm(s: str) -> str:
    return re.sub(r"[\s:：;|]+", "", s).lower()


def _label_score(label_part: str, key: str) -> float:
    n = _norm(label_part)
    if not n:
        return 0.0
    best = 0.0
    for lab in LABELS[key]:
        l = _norm(lab)
        if l in n:
            best = max(best, 0.7 + 0.3 * len(l) / len(n))
        elif len(n) >= 4 and n in l:
            best = max(best, 0.6 + 0.3 * len(n) / len(l))
        best = max(best, difflib.SequenceMatcher(None, n, l).ratio())
    return best


def _split_label(text: str):
    m = re.search(r"\s*[:：;]\s*", text)
    if not m:
        return None, text
    return text[: m.start()], text[m.end():]


def _year(y: int) -> int:
    if y < 100:
        return 2000 + y if y <= 50 else 2500 + y - 543
    return y - 543 if y > 2400 else y


def _fmt_dt(m) -> str:
    y, mo, d, hh, mm = _year(int(m.group(1))), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    return f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}"


def parse_slip(lines: List[Dict[str, Any]]) -> Dict[str, str]:
    f: Dict[str, str] = {}
    texts = [l["text"] for l in lines]
    last_key = None
    cold_idx = None
    for i, t in enumerate(texts):
        lab, val = _split_label(t)
        key = None
        if lab is not None:
            cands = []
            for k in LABELS:
                if k == "cold_time" and (cold_idx is None or i <= cold_idx):
                    continue
                cands.append((k, _label_score(lab, k)))
            cands.sort(key=lambda x: -x[1])
            if cands and cands[0][1] >= 0.62:
                key = cands[0][0]
        if key:
            if key == "cold_date":
                cold_idx = i
            if not f.get(key):
                f[key] = val.strip()
            last_key = key
            continue
        # บรรทัดต่อของชื่อวัตถุดิบ (ไม่มี ":")
        if last_key == "rm_name" and lab is None and t and not DT_RE.search(t) and len(t) < 60:
            f["rm_name"] = (f.get("rm_name", "") + " " + t).strip()
        last_key = None

    full = "\n".join(texts)
    # แผนการผลิต → code + line
    plan_src = f.get("plan", "") or full
    pm = PLAN_RE.search(" " + plan_src) or PLAN_RE.search(full)
    if pm:
        f["plan"] = f"{pm.group(1).upper()} ({pm.group(2).strip()})"
        f["code"] = pm.group(1).upper()
        f["line"] = pm.group(2).strip()
    # กะ
    sm = re.search(r"\b(DS|NS)\b", f.get("shift", "") or full, re.I)
    f["shift"] = sm.group(1).upper() if sm else ""
    # ตัวเลขล้วน
    if f.get("item"):
        m = re.search(r"\d{4,}", f["item"])
        f["item"] = m.group(0) if m else f["item"]
    if f.get("mat_code"):
        m = re.search(r"[A-Za-z0-9]{3,20}", f["mat_code"].replace(" ", ""))
        f["mat_code"] = m.group(0).upper() if m else ""
    for k in ("batch_tag", "batch_prep"):
        if f.get(k):
            m = re.search(r"[A-Za-z0-9\-]{5,}", f[k].replace(" ", ""))
            f[k] = m.group(0).upper() if m else f[k]
    tr = f.get("trays", "")
    m = re.search(r"(\d+)\s*ถาด", tr) or re.search(r"(\d+)", tr)
    kg = re.search(r"([\d.,]+)\s*(?:กิโล|kg|KG)", tr + " " + full)
    f["trays"] = m.group(1) if m else ""
    f["net_kg"] = kg.group(1).replace(",", "") if kg else ""
    # วันเวลาที่พิมพ์
    for k in ("out_big_chill", "prep_done"):
        m = DT_RE.search(f.get(k, ""))
        f[k] = _fmt_dt(m) if m else ""
    if not f["out_big_chill"] or not f["prep_done"]:
        dts = [_fmt_dt(m) for m in DT_RE.finditer(full)]
        if dts and not f["out_big_chill"]:
            f["out_big_chill"] = dts[0]
        if len(dts) > 1 and not f["prep_done"]:
            f["prep_done"] = dts[1]
    # วันเวลาเข้าห้องเย็น (เขียนมือ) — ดูเฉพาะบรรทัดตั้งแต่ช่อง "วันที่เข้าห้องเย็น" ลงไป
    f["cold_in"] = ""
    if cold_idx is not None:
        tail = texts[cold_idx: cold_idx + 4]
        date_txt = f.get("cold_date", "") + " " + " ".join(tail[1:2])
        dm = re.search(r"(\d{1,2})\s*[,./\-\s]\s*(\d{1,2})\s*[,./\-\s]\s*(\d{2,4})", date_txt)
        time_txt = f.get("cold_time", "") + " " + " ".join(tail[1:])
        tm = None
        cands = list(re.finditer(r"(?<!\d)(\d{1,2})[.:,](\d{2})(?!\d)", time_txt)) + \
            list(re.finditer(r"(?<!\d)(\d{1,2})\s*[.:,]\s*(\d{2})(?!\s*[,./\-]\s*\d)", time_txt))
        for mt in cands:
            h, mi = int(mt.group(1)), int(mt.group(2))
            if h < 24 and mi < 60:
                tm = (h, mi)
                break
        if dm and tm:
            d, mo, y = int(dm.group(1)), int(dm.group(2)), _year(int(dm.group(3)))
            if 1 <= d <= 31 and 1 <= mo <= 12:
                f["cold_in"] = f"{y:04d}-{mo:02d}-{d:02d} {tm[0]:02d}:{tm[1]:02d}"
        f["cold_in_raw"] = (f.get("cold_date", "") + " / " + f.get("cold_time", "")).strip(" /")
    # สำรอง: ถ้าหาหัวข้อไม่เจอ ดูวันที่ / เวลาแบบเขียนมือในบรรทัดท้าย ๆ (ใต้วันเวลาที่พิมพ์ล่าสุด)
    if not f["cold_in"]:
        last_dt = max([i for i, t in enumerate(texts) if DT_RE.search(t)] or [-1])
        tail = " \n ".join(texts[last_dt + 1:])
        dm = None
        for m in re.finditer(r"(?<![\d-])(\d{1,2})\s*[,./]\s*(\d{1,2})\s*[,./]\s*(\d{2,4})(?![\d:-])", tail):
            dm = m
        if dm:
            after = tail[dm.end():]
            tm = re.search(r"(?<!\d)(\d{1,2})[.:,](\d{2})(?!\d)", after) or \
                re.search(r"(?<![\d:])(\d{1,2}) ?[.,] ?(\d{2})(?!\d)", after)
            if tm and int(tm.group(1)) < 24 and int(tm.group(2)) < 60:
                d, mo, y = int(dm.group(1)), int(dm.group(2)), _year(int(dm.group(3)))
                if 1 <= d <= 31 and 1 <= mo <= 12:
                    f["cold_in"] = f"{y:04d}-{mo:02d}-{d:02d} {int(tm.group(1)):02d}:{int(tm.group(2)):02d}"
                    f["cold_in_raw"] = dm.group(0) + " / " + tm.group(0)
    # สำรอง: ชื่อวัตถุดิบ = บรรทัดที่มี (รหัส) และไม่ใช่แผนการผลิต
    if not f.get("rm_name"):
        for i, t in enumerate(texts):
            m = re.search(r"\(([A-Za-z0-9]{2,20})\)", t)
            if m and m.group(1).upper() != f.get("line", "").upper() and not PLAN_RE.fullmatch(t.strip()):
                if f.get("code") and f["code"] in t:
                    continue
                name = _split_label(t)[1].strip()
                if i + 1 < len(texts) and ":" not in texts[i + 1] and re.fullmatch(r"[A-Z0-9 ,.&\-/]{3,40}", texts[i + 1].strip()):
                    name += " " + texts[i + 1].strip()
                f["rm_name"] = name
                if not f.get("mat_code"):
                    f["mat_code"] = m.group(1).upper()
                break
    return f


# ============================ APP ============================
app = FastAPI(title="RM Delay")
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/api/health")
def health():
    return {"ok": True, "ocr": "ready" if _ocr is not None else ("error" if _ocr_err else "loading"),
            "ocr_error": _ocr_err, "time": datetime.now().isoformat(timespec="seconds")}


# ---------- delays ----------
@app.get("/api/delays")
def list_delays(limit: int = Query(100, ge=1, le=5000), offset: int = 0,
                date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to")):
    with db() as con:
        if date_from and date_to:
            rows = con.execute("SELECT * FROM delays WHERE date>=? AND date<=? ORDER BY ts DESC LIMIT ?",
                               (date_from, date_to, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM delays ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [row_to_rec(r) for r in rows]


def _save_photo(rec_id: str, data_url: str):
    m = re.match(r"data:image/\w+;base64,(.+)$", data_url or "", re.S)
    if not m:
        return False
    with open(os.path.join(PHOTO_DIR, rec_id + ".jpg"), "wb") as fh:
        fh.write(base64.b64decode(m.group(1)))
    return True


@app.post("/api/delays")
async def add_delay(req: Request):
    body = await req.json()
    rec = body.get("rec") or {}
    rid = str(rec.get("id", ""))
    if not SAFE_ID.match(rid):
        raise HTTPException(400, "id ไม่ถูกต้อง")
    if not rec.get("ts") or not rec.get("date"):
        raise HTTPException(400, "ต้องมี ts และ date")
    photo = body.get("photo")
    if photo:
        rec["hasPhoto"] = _save_photo(rid, photo)
    rec.pop("id", None)
    with _db_lock, db() as con:
        con.execute("INSERT OR REPLACE INTO delays(id,ts,date,shift,delay,data,created) VALUES(?,?,?,?,?,?,?)",
                    (rid, rec["ts"], rec["date"], rec.get("shift", ""), 1 if rec.get("delay", True) else 0,
                     json.dumps(rec, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
    return {"ok": True, "id": rid}


@app.delete("/api/delays/{rid}")
def delete_delay(rid: str, pin: str = ""):
    if pin != ADMIN_PIN:
        raise HTTPException(403, "PIN ไม่ถูกต้อง")
    if not SAFE_ID.match(rid):
        raise HTTPException(400, "id ไม่ถูกต้อง")
    with _db_lock, db() as con:
        con.execute("DELETE FROM delays WHERE id=?", (rid,))
    p = os.path.join(PHOTO_DIR, rid + ".jpg")
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@app.get("/api/photo/{rid}")
def get_photo(rid: str):
    if not SAFE_ID.match(rid):
        raise HTTPException(400, "id ไม่ถูกต้อง")
    p = os.path.join(PHOTO_DIR, rid + ".jpg")
    if not os.path.exists(p):
        raise HTTPException(404, "ไม่พบรูป")
    return FileResponse(p, media_type="image/jpeg")


CSV_COLS = [("ID", "id"), ("วันผลิต", "date"), ("กะ", "shift"), ("เวลาบันทึก", "ts"), ("รหัสวัตถุดิบ (mat)", "matCode"),
            ("เลขรายการ", "item"), ("แผนการผลิต", "plan"), ("วัตถุดิบ (สลิป)", "rm"), ("ประเภทตาม WI", "mat"),
            ("กลุ่ม", "group"), ("Code", "code"), ("Line", "line"), ("ห้องเย็น", "area"), ("สาเหตุ", "cause"),
            ("Batch/Lot", "lot"), ("เวลาเข้าห้องเย็น", "inTime"), ("อยู่ห้องเย็น (นาที)", "elapsedMin"),
            ("เกณฑ์ (ชม.)", "limitH"), ("สถานะ", "status"), ("Delay", "delay"), ("ผู้แจ้ง", "reporter"),
            ("มีรูป", "hasPhoto")]


def _fmt_iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return v or ""


@app.get("/api/export.csv")
def export_csv(date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to")):
    with db() as con:
        if date_from and date_to:
            rows = con.execute("SELECT * FROM delays WHERE date>=? AND date<=? ORDER BY ts", (date_from, date_to)).fetchall()
        else:
            rows = con.execute("SELECT * FROM delays ORDER BY ts").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in CSV_COLS])
    for r in rows:
        d = row_to_rec(r)
        out = []
        for _, k in CSV_COLS:
            v = d.get(k, "")
            if k in ("ts", "inTime"):
                v = _fmt_iso(v) if v else ""
            elif k in ("delay", "hasPhoto"):
                v = "Y" if v else "N"
            out.append(v)
        w.writerow(out)
    name = f"rm_delay_{date_from or 'all'}_{date_to or ''}.csv".replace("_.csv", ".csv")
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- materials / config ----------
@app.get("/api/materials")
def get_materials():
    with db() as con:
        rows = con.execute("SELECT code,name,wi FROM materials").fetchall()
    return {r["code"]: {"name": r["name"], "wi": r["wi"]} for r in rows}


@app.put("/api/materials/{code}")
async def put_material(code: str, req: Request):
    body = await req.json()
    code = code.strip().upper()
    if not re.match(r"^[A-Z0-9\-_.]{2,24}$", code):
        raise HTTPException(400, "รหัสไม่ถูกต้อง")
    with _db_lock, db() as con:
        con.execute("INSERT OR REPLACE INTO materials(code,name,wi,updated) VALUES(?,?,?,?)",
                    (code, str(body.get("name", ""))[:200], str(body.get("wi", ""))[:20],
                     datetime.now().isoformat(timespec="seconds")))
    return {"ok": True}


@app.get("/api/config")
def get_config():
    with db() as con:
        rows = con.execute("SELECT key,value FROM config").fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


@app.put("/api/config")
async def put_config(req: Request):
    body = await req.json()
    with _db_lock, db() as con:
        for k in ("areas", "lines"):
            if isinstance(body.get(k), list):
                con.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                            (k, json.dumps([str(x)[:40] for x in body[k]][:100], ensure_ascii=False)))
    return {"ok": True}


# ---------- OCR ----------
@app.post("/api/ocr")
def ocr_slip(image: UploadFile = File(...)):
    import numpy as np
    from PIL import Image, ImageOps

    raw = image.file.read()
    if not raw:
        raise HTTPException(400, "ไม่มีรูป")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        raise HTTPException(400, "เปิดรูปไม่ได้")
    mx = max(im.size)
    if mx > 2000:
        sc = 2000 / mx
        im = im.resize((int(im.width * sc), int(im.height * sc)))
    if SAVE_SLIPS:
        try:
            im.save(os.path.join(SLIP_DIR, datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg"), quality=80)
        except Exception:
            pass
    t0 = time.time()
    with _ocr_lock:
        if _ocr is None and not _ocr_err:
            _init_ocr()
        if _ocr is None:
            raise HTTPException(503, _ocr_err or "OCR ยังไม่พร้อม")
        arr = np.array(im)[:, :, ::-1].copy()  # RGB → BGR
        boxes = _run_ocr(arr)
    lines = group_lines(boxes)
    fields = parse_slip(lines)
    return {"fields": fields, "lines": [l["text"] for l in lines], "ms": int((time.time() - t0) * 1000)}


# ---------- app files ----------
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    ssl = os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY)
    print("=" * 60)
    print(" RM Delay server")
    print(f" ข้อมูล: {DATA_DIR}")
    print(f" เปิดที่: {'https' if ssl else 'http'}://<IP เครื่องนี้>:{PORT}")
    if not ssl:
        print(" ! ไม่พบ cert — มือถือจะเปิดกล้องไม่ได้ ให้รัน: python gen_cert.py <IP เครื่องนี้>")
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT,
                ssl_certfile=SSL_CERT if ssl else None, ssl_keyfile=SSL_KEY if ssl else None)
