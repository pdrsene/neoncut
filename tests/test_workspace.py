"""Workspace/deployment regression tests; isolated SQLite and synthetic media only."""
import os
import tempfile
import time
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
tmp=tempfile.TemporaryDirectory()
os.environ["DB_DIR"]=tmp.name
os.environ["ADMIN_EMAILS"]=""
from server import accounts as A, creator as C, workspace as W
def user():
    return A.db().execute("INSERT INTO users(email,name,created) VALUES(?,?,?)",(str(time.time_ns())+"@test.invalid","Test",time.time())).lastrowid
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.uid=user();self.req=SimpleNamespace(cookies={})
        self.u=lambda uid:A.db().execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
        self.auth=patch.object(A,"current_user",side_effect=lambda *args,**kw:self.u(self.uid));self.auth.start();self.addCleanup(self.auth.stop)
    def test_calendar_owner(self):
        item=W.CalendarItem(day="2026-11-20",platform="TikTok",title="Corte")
        i=W.calendar_add(item,self.req)["id"]
        self.assertEqual(W.calendar(self.req)[0]["title"],"Corte")
        self.uid=user()
        self.assertEqual(W.calendar(self.req),[])
        with self.assertRaises(HTTPException):W.calendar_delete(i,self.req)
        with self.assertRaises(HTTPException):W.calendar_edit(i,item,self.req)
    def test_opportunity_admin(self):
        o=W.Opportunity(title="Editor",creator="Teste",platform="Twitch",requirements="Autorizado",url="https://example.com/apply")
        with self.assertRaises(HTTPException):W.opportunity_add(o,self.req)
        with patch.object(A,"require_admin",return_value=self.u(self.uid)):
            i=W.opportunity_add(o,self.req)["id"]
            self.assertTrue(any(x["id"]==i for x in W.opportunities(self.req)))
            W.opportunity_delete(i,self.req)
            self.assertFalse(any(x["id"]==i for x in W.opportunities(self.req)))
        with self.assertRaises(HTTPException):W.opportunity_values(o.model_copy(update={"url":"javascript:alert(1)"}))
    def test_missions_dedup(self):
        for i in range(5):A.db().execute("INSERT INTO creator_xp VALUES(?,?,?,?,?)",(self.uid,"clip",str(i),10,time.time()))
        W.missions(self.uid); p=C.points(self.uid)
        self.assertEqual(p,100)
        W.missions(self.uid);self.assertEqual(C.points(self.uid),p)
    def test_cloud_real_ffmpeg_and_private(self):
        jid=W.reserve_cloud(self.uid,"free",0,1,"vertical")
        folder,src,out=W.cloud_paths(jid);folder.mkdir()
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=c=blue:s=320x240:r=15","-t","2","-c:v","libx264","-threads","1","-pix_fmt","yuv420p",str(src)],check=True,timeout=20)
        A.db().execute("UPDATE cloud_exports SET status='running' WHERE id=?",(jid,))
        W.run_cloud(A.db().execute("SELECT * FROM cloud_exports WHERE id=?",(jid,)).fetchone())
        row=A.db().execute("SELECT * FROM cloud_exports WHERE id=?",(jid,)).fetchone()
        self.assertEqual(row["status"],"done",row["error"])
        self.assertTrue(out.is_file()); self.assertFalse(src.exists())
        self.assertEqual(W.cloud_file(jid,self.req).path,out)
        with self.assertRaises(HTTPException):W.reserve_cloud(self.uid,"free",0,1,"vertical")
        self.uid=user()
        with self.assertRaises(HTTPException):W.cloud_file(jid,self.req)
    def test_invalid_cloud(self):
        for vals in [(float("nan"),1,"vertical"),(0,90,"original"),(0,1,"bad")]:
            with self.assertRaises(HTTPException):W.validate_cloud(*vals)
    def test_payment_and_conversion(self):
        p={"id":123456,"currency_id":"BRL","status":"approved","external_reference":f"{self.uid}:pro:1290","transaction_amount":12.9}
        with patch.object(A,"MP_TOKEN","test"),patch.object(A,"http_json",return_value=p):
            self.assertEqual(A.apply_payment("123456"),"approved")
            before=self.u(self.uid)["pro_until"]
            self.assertEqual(A.apply_payment("123456"),"approved")
            self.assertEqual(self.u(self.uid)["pro_until"],before)
        self.assertEqual(len(A.billing_conversions(self.req)["purchases"]),1)
        self.assertEqual(len(A.billing_conversions(self.req)["purchases"]),1)
        other=self.uid;self.uid=user()
        A.conversion_ack(A.ConversionAck(transaction_id="123456"),self.req)
        self.uid=other
        self.assertEqual(len(A.billing_conversions(self.req)["purchases"]),1)
        A.conversion_ack(A.ConversionAck(transaction_id="123456"),self.req)
        self.assertEqual(A.billing_conversions(self.req)["purchases"],[])
    def test_webhook_signature(self):
        import hmac,hashlib
        secret="test"; msg="id:123;request-id:req;ts:123456;"
        sig=hmac.new(secret.encode(),msg.encode(),hashlib.sha256).hexdigest()
        self.assertTrue(A.valid_mp_signature(secret,"ts=123456,v1="+sig,"req","123"))
        self.assertFalse(A.valid_mp_signature(secret,"ts=123456,v1="+sig,"req","124"))
        self.assertFalse(A.valid_mp_signature(secret,"invalid","req","123"))
    def test_payment_invalid(self):
        for i,changes,expected in [(123457,{"currency_id":"USD"},"invalid"),(123458,{"transaction_amount":1},"amount_mismatch"),(123459,{"id":1},"invalid")]:
            p={"id":i,"currency_id":"BRL","status":"approved","external_reference":f"{self.uid}:pro:1290","transaction_amount":12.9,**changes}
            with patch.object(A,"MP_TOKEN","test"),patch.object(A,"http_json",return_value=p):self.assertEqual(A.apply_payment(str(i)),expected)
        self.assertEqual(self.u(self.uid)["pro_until"],0)
if __name__=="__main__":unittest.main()
