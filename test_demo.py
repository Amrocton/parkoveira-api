"""Run with the server venv: python -m unittest test_demo (from server/)."""
import concurrent.futures
import json
import gzip
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

HERE = Path(__file__).resolve().parent


class DemoTest(unittest.TestCase):
    def request(self, path, data=None, token=None, method=None):
        headers = {"Content-Type":"application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(self.url+path, None if data is None else json.dumps(data).encode(), headers, method=method)
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/"test.sqlite"
            source = Path(os.environ.get("PARKING_CATALOG", HERE/"parking.sqlite.gz"))
            with (gzip.open(source, "rb") if source.suffix == ".gz" else source.open("rb")) as catalog, target.open("wb") as out:
                shutil.copyfileobj(catalog, out)
            pid = "623:2755298624"
            with sqlite3.connect(target) as db:
                db.execute("UPDATE parking SET capacity=1 WHERE id=?",(pid,))
            db.close()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1",0))
                port=sock.getsockname()[1]
            self.url=f"http://127.0.0.1:{port}/api/v1"
            proc=subprocess.Popen([sys.executable,"run.py"],cwd=HERE,env=dict(os.environ,PARKING_DATABASE=str(target),PARKING_PORT=str(port),PARKING_BIND="127.0.0.1"),stdout=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        if self.request("/health")[0]==200:
                            break
                    except URLError:
                        time.sleep(.1)
                credentials={"login":"demo@parkoveira.test","password":"DemoPark123"}
                token=self.request("/auth/login",credentials)[1]["access_token"]
                phone={"login":"8 (999) 123-45-67","password":"DemoTest123"}
                other=self.request("/auth/register",phone)[1]["access_token"]
                phone["login"]="+79991234567"
                self.assertEqual(self.request("/auth/login",phone)[0],200)
                self.assertEqual(self.request("/auth/register",phone)[0],409)
                car=self.request("/vehicles",token=token)[1][0]["id"]
                start=int(time.time())
                b={"parking_id":pid,"vehicle_id":car,"starts_at":start,"ends_at":start+3600}
                self.assertEqual(self.request("/quotes",b,other)[0],404)
                price=self.request("/quotes",b,token)[1]["amount_kopecks"]
                attempts=[dict(b,amount_kopecks=price,request_id=str(uuid4())) for _ in range(2)]
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    results=list(pool.map(lambda item:self.request("/bookings",item,token),attempts))
                self.assertEqual(sorted(r[0] for r in results),[200,409])
                idx=next(i for i,r in enumerate(results) if r[0]==200)
                booking=results[idx][1]
                self.assertEqual(self.request("/bookings",attempts[idx],token)[1]["id"],booking["id"])
                path="/bookings/"+booking["id"]
                self.assertEqual(self.request(path,token=other)[0],404)
                self.assertEqual(self.request("/bookings",token=other)[1],[])
                self.assertEqual(self.request("/parkings/"+pid)[1]["free_spaces"],0)
                self.assertEqual(self.request(path+"/cancel",{},token)[1]["status"],"cancelled")
                self.assertEqual(self.request("/parkings/"+pid)[1]["free_spaces"],1)
                self.assertEqual(self.request("/vehicles/"+str(car),token=token,method="DELETE")[0],200)
                self.assertEqual(self.request("/bookings",token=token)[1][0]["plate"],"А123ВС77")
                self.assertEqual(self.request("/auth/logout",{},token)[0],200)
                self.assertEqual(self.request("/me",token=token)[0],401)
            finally:
                proc.terminate()
                proc.wait(timeout=10)

