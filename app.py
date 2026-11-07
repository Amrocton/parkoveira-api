"""Parkoveira prototype API. All timestamps are Unix seconds."""
import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import holidays
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

DB = Path(os.environ.get("PARKING_DATABASE", Path(__file__).with_name("demo.sqlite")))
MOSCOW = timezone(timedelta(hours=3))
CALENDAR = holidays.Russia()
VehicleType = Literal["Легковой автомобиль", "Мотоцикл", "Грузовой автомобиль", "Автобус"]


def fail(code, message):
    raise HTTPException(code, message)


@contextmanager
def connection(write=False):
    db = sqlite3.connect(DB.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app):
    with connection(True) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            for sql in Path(__file__).with_name("migration.sql").read_text(encoding="utf8").split(";"):
                if sql.strip():
                    db.execute(sql)
            db.execute("PRAGMA user_version=1")
    yield


app = FastAPI(title="Parkoveira Demo", version="1", lifespan=lifespan)
from fastapi import APIRouter
api = APIRouter(prefix="/api/v1")


class Credentials(BaseModel):
    login: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


def contact(value):
    value = value.strip().lower()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return value
    number = re.sub(r"[\s()-]", "", value)
    if re.fullmatch(r"8\d{10}", number):
        number = "+7" + number[1:]
    if re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
        return number
    fail(422, "Введите почту или телефон с кодом страны")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)


def session(db, uid):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO session VALUES(?,?,?)", (digest(token), uid, int(time.time()) + 604800))
    return {"access_token": token, "token_type": "bearer"}


@api.post("/auth/register", status_code=201)
def register(body: Credentials):
    login = contact(body.login)
    salt = secrets.token_bytes(16)
    hashed = password_hash(body.password, salt)
    with connection(True) as db:
        if db.execute("SELECT 1 FROM account WHERE login=?", (login,)).fetchone():
            fail(409, "Этот контакт уже зарегистрирован")
        uid = db.execute("INSERT INTO account(login,salt,hash) VALUES(?,?,?)", (login,salt,hashed)).lastrowid
        return session(db, uid)


@api.post("/auth/login")
def login(body: Credentials):
    with connection() as db:
        row = db.execute("SELECT * FROM account WHERE login=?", (contact(body.login),)).fetchone()
    if not row or not hmac.compare_digest(password_hash(body.password, row["salt"]), row["hash"]):
        fail(401, "Неверный логин или пароль")
    with connection(True) as db:
        return session(db, row["id"])


def identity(authorization: str = Header(default="")):
    scheme, _, token = authorization.partition(" ")
    with connection() as db:
        row = db.execute("SELECT user_id FROM session WHERE token=? AND expires>unixepoch()", (digest(token),)).fetchone()
    if scheme.lower() != "bearer" or not row:
        fail(401, "Требуется вход")
    return row[0], digest(token)


@api.post("/auth/logout")
def logout(auth=Depends(identity)):
    with connection(True) as db:
        db.execute("DELETE FROM session WHERE token=?", (auth[1],))
    return {"ok": True}


@api.get("/me")
def me(auth=Depends(identity)):
    with connection() as db:
        return dict(db.execute("SELECT id,login FROM account WHERE id=?", (auth[0],)).fetchone())


class Vehicle(BaseModel):
    plate: str = Field(min_length=2, max_length=24)
    vehicle_type: VehicleType


@api.get("/vehicles")
def vehicles(auth=Depends(identity)):
    with connection() as db:
        return [dict(r) for r in db.execute("SELECT id,plate,vehicle_type FROM vehicle WHERE user_id=? AND active=1 ORDER BY id", (auth[0],))]


def save_vehicle(body, uid, vid=None):
    plate = body.plate.strip().upper()
    norm = re.sub(r"[\s-]", "", plate).translate(str.maketrans("ABEKMHOPCTYX","АВЕКМНОРСТУХ"))
    if not (norm.isalnum() and len(norm)>=2):
        fail(422, "Проверьте госномер")
    with connection(True) as db:
        try:
            if vid is None:
                vid = db.execute("INSERT INTO vehicle(user_id,plate,normalized,vehicle_type) VALUES(?,?,?,?)",
                                 (uid,plate,norm,body.vehicle_type)).lastrowid
            elif not db.execute("UPDATE vehicle SET plate=?,normalized=?,vehicle_type=? WHERE id=? AND user_id=? AND active=1",
                                (plate,norm,body.vehicle_type,vid,uid)).rowcount:
                fail(404, "Транспорт не найден")
        except sqlite3.IntegrityError:
            fail(409, "Этот госномер уже добавлен")
    return {"id":vid, "plate":plate, "vehicle_type":body.vehicle_type}


@api.post("/vehicles", status_code=201)
def add(body: Vehicle, auth=Depends(identity)):
    return save_vehicle(body, auth[0])


@api.patch("/vehicles/{vid}")
def edit(vid:int, body:Vehicle, auth=Depends(identity)):
    return save_vehicle(body, auth[0], vid)


@api.delete("/vehicles/{vid}")
def remove(vid:int, auth=Depends(identity)):
    with connection(True) as db:
        if not db.execute("UPDATE vehicle SET active=0 WHERE id=? AND user_id=? AND active=1",(vid,auth[0])).rowcount:
            fail(404, "Транспорт не найден")
    return {"ok":True}


@api.get("/health")
def health():
    with connection() as db:
        db.execute("SELECT id FROM parking LIMIT 1").fetchone()
    return {"status":"ok"}


@api.get("/parkings")
def nearby(lat:float=Query(55.757689,ge=-90,le=90), lon:float=Query(37.826719,ge=-180,le=180),
           radius:int=Query(1200,ge=1,le=20000), q:str=Query("",max_length=160), limit:int=Query(50,ge=1,le=200)):
    north = radius / 111195
    east = north / max(0.00001, math.cos(math.radians(lat)))
    with connection() as db:
        rows = db.execute("SELECT * FROM parking_now WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?",
                          (lat-north,lat+north,lon-east,lon+east)) if not q.strip() else db.execute("SELECT * FROM parking_now")
        result = []
        for row in rows:
            if q.strip().casefold() not in (row["name"]+" "+(row["address"] or "")).casefold():
                continue
            dy = math.radians(row["latitude"]-lat)
            dx = math.radians(row["longitude"]-lon)
            a = math.sin(dy/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(row["latitude"]))*math.sin(dx/2)**2
            distance = 12742000 * math.asin(min(1, math.sqrt(a)))
            if q.strip() or distance <= radius:
                result.append(dict(row, distance_m=round(distance)))
    return sorted(result,key=lambda r:(r["distance_m"],r["id"]))[:limit]


@api.get("/parkings/{pid}")
def detail(pid:str):
    import json
    with connection() as db:
        row = db.execute("SELECT * FROM parking_now WHERE id=?", (pid,)).fetchone()
        if not row:
            fail(404,"Парковка не найдена")
        return dict(row, tariffs=[json.loads(r[0]) for r in db.execute("SELECT source_rule_json FROM tariff WHERE parking_id=? ORDER BY rule_no",(pid,))])


class Interval(BaseModel):
    parking_id:str = Field(max_length=64)
    vehicle_id:int
    starts_at:int = Field(gt=0)
    ends_at:int = Field(gt=0)


class Purchase(Interval):
    request_id:UUID
    amount_kopecks:int = Field(ge=0)


def quote(db, uid, b):
    now = int(time.time())
    duration = b.ends_at-b.starts_at
    if not (now-60 <= b.starts_at <= now+365*86400 and 60 <= duration <= 86400 and duration%60==0):
        fail(422,"Выберите от 1 минуты до 24 часов в течение ближайшего года")
    vehicle = db.execute("SELECT * FROM vehicle WHERE id=? AND user_id=? AND active=1",(b.vehicle_id,uid)).fetchone()
    if not vehicle:
        fail(404,"Транспорт не найден")
    p = db.execute("SELECT capacity FROM parking WHERE id=?",(b.parking_id,)).fetchone()
    if not p:
        fail(404,"Парковка не найдена")
    events = []
    for start,end in db.execute("SELECT starts_at,ends_at FROM booking WHERE parking_id=? AND cancelled_at IS NULL AND starts_at<? AND ends_at>?",(b.parking_id,b.ends_at,b.starts_at)):
        events.extend([(max(start,b.starts_at),1),(min(end,b.ends_at),-1)])
    occupied = peak = 0
    for _,delta in sorted(events):
        occupied += delta
        peak = max(occupied,peak)
    if p[0] is None or peak >= p[0]:
        fail(409,"На выбранное время нет доступного места")
    rules = db.execute("SELECT * FROM tariff WHERE parking_id=? AND vehicle_type=?",(b.parking_id,vehicle["vehicle_type"])).fetchall()
    total = 0
    current = b.starts_at
    while current < b.ends_at:
        dt = datetime.fromtimestamp(current,MOSCOW)
        period = "праздничные дни" if dt.date() in CALENDAR else ("выходные дни" if dt.weekday()>=5 else "будни")
        clock = dt.strftime("%H:%M")
        prices = set()
        for r in rules:
            if r["period"] not in (period,"ежедневно"):
                continue
            window = r["time_range"]
            if window not in ("круглосуточно","00:00-00:00"):
                start,end = window.split("-")
                if end=="23:59":
                    end="24:00"
                if not (start<=clock<end if start<end else clock>=start or clock<end):
                    continue
            if r["tariff_type"]!="фиксированный тариф" or r["hour_price_rub"] is None:
                fail(422,"Этот тариф доступен только для просмотра")
            prices.add(round(r["hour_price_rub"]*100))
        if len(prices)!=1:
            fail(422,"Для выбранного транспорта и времени нет однозначной почасовой цены")
        seconds = min(60-current%60,b.ends_at-current)
        total += prices.pop()*seconds
        current += seconds
    return (total+1800)//3600, vehicle


@api.post("/quotes")
def preview(b:Interval, auth=Depends(identity)):
    with connection() as db:
        price,_ = quote(db,auth[0],b)
    return {"amount_kopecks":price,"currency":"RUB"}


HISTORY = """SELECT b.id,b.vehicle_id,b.parking_id,p.name,p.address,b.plate,b.vehicle_type,
 b.starts_at,b.ends_at,b.amount_kopecks,b.cancelled_at,t.paid_at,
 CASE WHEN b.cancelled_at IS NOT NULL THEN 'cancelled' WHEN b.ends_at<=unixepoch() THEN 'finished'
 WHEN b.starts_at>unixepoch() THEN 'upcoming' ELSE 'active' END AS status
 FROM booking b JOIN parking p ON p.id=b.parking_id LEFT JOIN payment t ON t.booking_id=b.id WHERE b.user_id=?"""


def receipt(db,uid,bid):
    row=db.execute(HISTORY+" AND b.id=?",(uid,bid)).fetchone()
    if not row:
        fail(404,"Бронь не найдена")
    return dict(row,simulated_payment=True)


@api.post("/bookings")
def purchase(b:Purchase, auth=Depends(identity)):
    uid=auth[0]
    with connection(True) as db:
        previous=db.execute("SELECT * FROM booking WHERE user_id=? AND request_id=?",(uid,str(b.request_id))).fetchone()
        if previous:
            if any(previous[k]!=getattr(b,k) for k in ("parking_id","vehicle_id","starts_at","ends_at","amount_kopecks")):
                fail(409,"Идентификатор запроса уже использован")
            return receipt(db,uid,previous["id"])
        price,v=quote(db,uid,b)
        if price!=b.amount_kopecks:
            fail(409,"Цена изменилась, запросите расчёт ещё раз")
        bid=str(uuid4())
        db.execute("""INSERT INTO booking(id,parking_id,vehicle_type,starts_at,ends_at,amount_kopecks,user_id,vehicle_id,plate,request_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(bid,b.parking_id,v["vehicle_type"],b.starts_at,b.ends_at,price,uid,v["id"],v["plate"],str(b.request_id)))
        db.execute("INSERT INTO payment VALUES(?,unixepoch(),?)",(bid,price))
        return receipt(db,uid,bid)


@api.get("/bookings")
def history(auth=Depends(identity)):
    with connection() as db:
        return [dict(r, simulated_payment=True) for r in db.execute(HISTORY+" ORDER BY b.starts_at DESC,b.id",(auth[0],))]


@api.get("/bookings/{bid}")
def one(bid:str, auth=Depends(identity)):
    with connection() as db:
        return receipt(db,auth[0],bid)


@api.post("/bookings/{bid}/cancel")
def cancel(bid:str, auth=Depends(identity)):
    with connection(True) as db:
        r=receipt(db,auth[0],bid)
        if r["status"]=="finished":
            fail(409,"Бронь уже завершена")
        db.execute("UPDATE booking SET cancelled_at=coalesce(cancelled_at,unixepoch()) WHERE id=?",(bid,))
        return receipt(db,auth[0],bid)


app.include_router(api)

