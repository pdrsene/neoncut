"""Regression tests for the private projects library and creator characters.
All test data stays in a temporary database, never in production.
"""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

tmp=tempfile.TemporaryDirectory()
os.environ["DB_DIR"]=tmp.name
os.environ["ADMIN_EMAILS"]=""
from fastapi import HTTPException
from server import accounts as A, creator as C

def user():
    return A.db().execute("INSERT INTO users(email,name,created) VALUES(?,?,?)",
                          (str(time.time_ns())+"@test.invalid","Library Test",time.time())).lastrowid

class LibraryAndPixelTests(unittest.TestCase):
    def setUp(self):
        self.uid=user()
        self.req=SimpleNamespace(cookies={})
        self.auth=patch.object(A,"current_user",return_value={"id":self.uid})
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.pid=A.db().execute(
            "INSERT INTO projects(user_id,created,title,kind,source,clips,meta) VALUES(?,?,?,?,?,?,?)",
            (self.uid,time.time(),"Primeiro vídeo","file","video.mp4",'[{"start":0,"end":20}]',
             '{"settings":{"turbo":true}}')).lastrowid

    def test_library_favorite_folder_rename_and_autosave_merge(self):
        res=A.update_project_library(self.pid,A.ProjectLibraryUpdate(
            favorite=True,folder="Gameplays",title="Melhor gameplay"),self.req)
        self.assertEqual(res["title"],"Melhor gameplay")
        self.assertTrue(res["library"]["favorite"])
        self.assertEqual(res["library"]["folder"],"Gameplays")
        A.update_project(self.pid,A.ProjectUpdate(
            meta={"settings":{"turbo":False,"tpl":"stream"}},clips=[{"start":1,"end":18}]),self.req)
        p=A.list_projects(self.req)[0]
        self.assertTrue(p["meta"]["library"]["favorite"])
        self.assertEqual(p["meta"]["library"]["folder"],"Gameplays")
        self.assertFalse(p["meta"]["settings"]["turbo"])
        self.assertEqual(len(p["clips"]),1)

    def test_library_archive_restore_and_permissions(self):
        A.update_project_library(self.pid,A.ProjectLibraryUpdate(archived=True),self.req)
        self.assertTrue(A.list_projects(self.req)[0]["meta"]["library"]["archived"])
        A.update_project_library(self.pid,A.ProjectLibraryUpdate(archived=False),self.req)
        self.assertFalse(A.list_projects(self.req)[0]["meta"]["library"]["archived"])
        for body in [A.ProjectLibraryUpdate(folder="Não existe"),A.ProjectLibraryUpdate(title=" ")]:
            with self.assertRaises(HTTPException):
                A.update_project_library(self.pid,body,self.req)
        with self.assertRaises(HTTPException) as e:
            A.update_project_library(self.pid+9999,A.ProjectLibraryUpdate(favorite=True),self.req)
        self.assertEqual(e.exception.status_code,404)

    def test_pagination_and_delete_preserves_quota(self):
        for i in range(70):
            A.db().execute("INSERT INTO projects(user_id,created,title) VALUES(?,?,?)",
                          (self.uid,time.time()+i,"Teste "+str(i)))
        page1=A.list_projects(self.req,limit=60,offset=0)
        page2=A.list_projects(self.req,limit=60,offset=60)
        self.assertEqual(len(page1),60)
        self.assertEqual(len(page2),11)
        self.assertEqual(len({p["id"] for p in page1+page2}),71)
        A.delete_project(self.pid,self.req)
        old=A.db().execute("SELECT clips,meta FROM projects WHERE id=?",(self.pid,)).fetchone()
        self.assertEqual(json.loads(old["clips"]),[])
        self.assertTrue(json.loads(old["meta"])["hidden"])
        with self.assertRaises(HTTPException):
            A.update_project_library(self.pid,A.ProjectLibraryUpdate(favorite=True),self.req)

    def test_character_selection_level_gate_and_public_profile(self):
        d=C.me(self.req)
        self.assertEqual(d["avatar_id"],"rookie")
        self.assertEqual(len(d["avatars"]),10)
        self.assertEqual(C.set_avatar(C.AvatarPick(avatar_id="fox"),self.req)["avatar_id"],"fox")
        with self.assertRaises(HTTPException) as e:
            C.set_avatar(C.AvatarPick(avatar_id="dragon"),self.req)
        self.assertEqual(e.exception.status_code,403)
        A.db().execute("INSERT INTO creator_xp(uid,kind,event_key,points,created) VALUES(?,?,?,?,?)",
                       (self.uid,"test","level",1500,time.time()))
        self.assertEqual(C.set_avatar(C.AvatarPick(avatar_id="dragon"),self.req)["avatar_id"],"dragon")
        self.assertEqual(C.me(self.req)["avatar_id"],"dragon")
        C.portfolio(C.Portfolio(name="Meu clipe",public=True),self.req)
        public=C.public_profile(C.me(self.req)["token"])
        self.assertEqual(public["avatar_id"],"dragon")
        self.assertNotIn("email",public)
        self.assertNotIn("token",public)

if __name__=="__main__":
    unittest.main()
