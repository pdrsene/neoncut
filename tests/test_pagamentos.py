"""Testes do pagamento (Mercado Pago simulado): liberação única, valor errado e evento purchase sem duplicar.
Rodar: python3 tests/test_pagamentos.py"""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

PAYS = {"1001": {"status": "approved", "transaction_amount": 12.9, "external_reference": "1:pro:1290", "payment_type_id": "bank_transfer"},
        "1002": {"status": "approved", "transaction_amount": 1.0, "external_reference": "1:pro:1290"},
        "1003": {"status": "pending", "transaction_amount": 12.9, "external_reference": "1:pro:1290"}}

for _k, _v in PAYS.items(): _v.update(id=int(_k), currency_id="BRL")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.05)
        pid = self.path.rsplit("/", 1)[-1]
        body = json.dumps(PAYS.get(pid, {})).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(DB_DIR=tempfile.mkdtemp(), MP_ACCESS_TOKEN="teste", MP_API_BASE=f"http://127.0.0.1:{srv.server_port}")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import accounts as A  # noqa

A.db().execute("INSERT INTO users(id,email,created) VALUES(1,'a@b.com',?)", (time.time(),))
# retorno do checkout e webhook ao mesmo tempo
res = []
ts = [threading.Thread(target=lambda: res.append(A.apply_payment("1001"))) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
u = A.db().execute("SELECT pro_until, plan FROM users WHERE id=1").fetchone()
days = (u["pro_until"] - time.time()) / 86400
assert all(r == "approved" for r in res), res
assert 29.9 < days < 30.1, f"liberou {days:.1f} dias (esperado 30)"
assert A.db().execute("SELECT COUNT(*) FROM events WHERE kind='purchase'").fetchone()[0] == 1
assert A.apply_payment("1002") == "amount_mismatch"
assert A.db().execute("SELECT status FROM payments WHERE id='1002'").fetchone()[0] == "amount_mismatch"
assert A.apply_payment("1003") == "pending"
assert A.db().execute("SELECT COUNT(*) FROM events WHERE kind='purchase'").fetchone()[0] == 1
assert A.apply_payment("abc") == "invalid"
print("OK: pagamento libera 30 dias uma única vez, valor errado não libera, purchase sem duplicar")
