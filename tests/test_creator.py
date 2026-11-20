"""Isolated tests: never use the Railway production database."""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

tmp = tempfile.TemporaryDirectory()
os.environ["DB_DIR"] = tmp.name
os.environ["ADMIN_EMAILS"] = ""
from server import accounts as A, creator as C
from fastapi import HTTPException

def new_user(name):
    return A.db().execute("INSERT INTO users(email,name,created) VALUES(?,?,?)",
                          (name+"@test.invalid",name,time.time())).lastrowid

class CreatorTests(unittest.TestCase):
    def setUp(self):
        self.uid=new_user("u"+str(time.time_ns()))
        self.req=SimpleNamespace(cookies={})
        self.auth=patch.object(A,"current_user",return_value={"id":self.uid})
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.pid=A.db().execute("INSERT INTO projects(user_id,created,clips) VALUES(?,?,?)",
                                  (self.uid,time.time(),json.dumps([{"start":0,"end":30}]*20))).lastrowid
    def test_duplicate_and_cap(self):
        claim=lambda i:C.clip_claim(C.ClipClaim(project_id=self.pid,index=i),self.req)
        self.assertEqual(claim(0)["added"],10)
        self.assertEqual(claim(0)["added"],0)
        for i in range(1,20): claim(i)
        self.assertEqual(C.points(self.uid),200)
        p2=A.db().execute("INSERT INTO projects(user_id,created,clips) VALUES(?,?,?)",
                          (self.uid,time.time(),'[{"start":0,"end":10}]')).lastrowid
        self.assertEqual(C.clip_claim(C.ClipClaim(project_id=p2,index=0),self.req)["added"],0)
    def test_ownership(self):
        other=new_user("other"+str(time.time_ns()))
        p=A.db().execute("INSERT INTO projects(user_id,created,clips) VALUES(?,?,?)",
                        (other,time.time(),'[{"start":0,"end":10}]')).lastrowid
        with self.assertRaises(HTTPException) as e:C.clip_claim(C.ClipClaim(project_id=p,index=0),self.req)
        self.assertEqual(e.exception.status_code,404)
    def test_portfolio_privacy_and_theme(self):
        row=C.ensure_profile(self.uid)
        with self.assertRaises(HTTPException):C.public_profile(row["token"])
        with self.assertRaises(HTTPException):C.portfolio(C.Portfolio(theme="legend"),self.req)
        with self.assertRaises(HTTPException):C.portfolio(C.Portfolio(links=[{"title":"x","url":"javascript:alert(1)"}]),self.req)
        C.portfolio(C.Portfolio(name="Creator",public=True,links=[{"title":"Video","url":"https://youtu.be/test"}]),self.req)
        pub=C.public_profile(row["token"])
        self.assertEqual(pub["name"],"Creator")
        self.assertNotIn("email",pub);self.assertNotIn("uid",pub);self.assertNotIn("token",pub)
        C.portfolio(C.Portfolio(name="Creator",public=False),self.req)
        with self.assertRaises(HTTPException):C.public_profile(row["token"])
    def test_referral_payment_and_dedup(self):
        row=C.ensure_profile(self.uid)
        invited=new_user("invite"+str(time.time_ns()))
        A.record_creator_invite(SimpleNamespace(cookies={"nc_invite":row["token"]}),invited)
        A.record_creator_invite(SimpleNamespace(cookies={"nc_invite":row["token"]}),self.uid)
        self.assertEqual(C.me(self.req)["invited"],1)
        self.assertEqual(C.points(self.uid),0)
        past=time.time()-8*86400
        A.db().execute("UPDATE creator_referrals SET created=? WHERE invited=?",(past-1,invited))
        A.db().execute("INSERT INTO payments(id,user_id,plan,amount,status,created) VALUES(?,?,?,?,?,?)",
                       ("pay"+str(invited),invited,"pro",12.9,"approved",past))
        self.assertEqual(C.me(self.req)["qualified"],1)
        self.assertEqual(C.me(self.req)["xp"],100)
        self.assertEqual(C.me(self.req)["xp"],100)
    def test_thresholds(self):
        self.assertEqual(C.level_info(99)["level"],1)
        self.assertEqual(C.level_info(100)["level"],2)
        self.assertEqual(C.level_info(1500)["title"],"Lenda")
    def test_unsafe_urls(self):
        for url in ("http://youtu.be/a","https://youtube.com.evil.invalid/x","https://youtube.com@evil.invalid/x"):
            self.assertFalse(C.safe_link(url))

if __name__=="__main__":
    unittest.main()
