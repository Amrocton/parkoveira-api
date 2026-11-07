"""Launch from any working directory. First run creates a working copy."""
import gzip
import os
import shutil
import socket
from pathlib import Path

import uvicorn

from app import DB, Credentials, Vehicle, add, app, connection, contact, lifespan, register
import asyncio


async def prepare():
    async with lifespan(app):
        with connection() as db:
            exists = db.execute("SELECT 1 FROM account WHERE login=?", (contact("demo@parkoveira.test"),)).fetchone()
        if not exists:
            register(Credentials(login="demo@parkoveira.test", password="DemoPark123"))
            with connection() as db:
                uid = db.execute("SELECT id FROM account WHERE login='demo@parkoveira.test'").fetchone()[0]
            add(Vehicle(plate="А123ВС77",vehicle_type="Легковой автомобиль"), (uid,None))


if __name__ == "__main__":
    if not DB.exists():
        source = Path(os.environ.get("PARKING_CATALOG", Path(__file__).with_name("parking.sqlite.gz")))
        if not source.is_file():
            raise SystemExit("Set PARKING_CATALOG to the original parking.sqlite path")
        temp = DB.with_suffix(".copying")
        with (gzip.open(source, "rb") if source.suffix == ".gz" else source.open("rb")) as catalog, temp.open("wb") as out:
            shutil.copyfileobj(catalog, out)
        temp.replace(DB)
    asyncio.run(prepare())
    port = int(os.environ.get("PARKING_PORT", "8080"))
    for ip in sorted(set(socket.gethostbyname_ex(socket.gethostname())[2])):
        print(f"API on host interface: http://{ip}:{port}", flush=True)
    uvicorn.run(app, host=os.environ.get("PARKING_BIND","0.0.0.0"), port=port)

