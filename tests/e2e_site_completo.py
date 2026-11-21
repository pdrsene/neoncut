# Teste completo do site: páginas, demonstração, editor, cadastro, download, áreas logadas, admin e nível máximo.
import asyncio, json, time
from playwright.async_api import async_playwright
B = "http://localhost:8765"
R = {}
def ok(k, v): R[k] = v
async def overflow(pg):
    return await pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        for name, opts in [("desk", {"viewport": {"width": 1366, "height": 900}}), ("mob", dict(p.devices["Pixel 7"]))]:
            ctx = await br.new_context(**opts, accept_downloads=True); pg = await ctx.new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append("JS: " + str(e)[:160]))
            pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url[-60:]}") if r.status >= 500 else None)
            await pg.goto(B + "/"); await pg.wait_for_timeout(3000)
            ok(name + " landing overflow px", await overflow(pg))
            ok(name + " vídeos animados na home", await pg.evaluate("[...document.querySelectorAll('#vLanding video')].filter(v=>v.getBoundingClientRect().width>0).map(v=>(v.id||v.currentSrc.split('/').pop())+':'+(v.paused?'PARADO':'tocando')).join(', ')"))
            for h in ["#como", "#precos", "#faq", "#termos", "#privacidade"]:
                await pg.goto(B + "/" + h); await pg.wait_for_timeout(500)
            await pg.goto(B + "/"); await pg.wait_for_timeout(1500)
            # demonstração
            await pg.locator("[data-demo-run]").first.click()
            await pg.wait_for_selector("#results:not(.hidden)", timeout=120000); await pg.wait_for_timeout(1500)
            ok(name + " demo cortes", await pg.locator("#grid .clip").count())
            # editor
            await pg.locator("#grid .clip [data-edit], #grid .clip button:has-text('Editar')").first.click(); await pg.wait_for_timeout(2500)
            ok(name + " editor aberto", await pg.is_visible("#tpls"))
            ok(name + " lista de fontes escondida", not await pg.is_visible("#titleStyles"))
            a1 = await pg.evaluate("document.querySelector('#tpls canvas').toDataURL().length"); await pg.wait_for_timeout(900)
            a2 = await pg.evaluate("document.querySelector('#tpls canvas').toDataURL()")
            await pg.wait_for_timeout(600); a3 = await pg.evaluate("document.querySelector('#tpls canvas').toDataURL()")
            ok(name + " miniaturas de template animadas", a2 != a3)
            await pg.screenshot(path=f"/tmp/full_{name}_editor.png")
            # cadastro + download
            email = f"t{int(time.time()*1000)}{name}@teste.com"
            await pg.click('#exportOne')
            await pg.wait_for_selector("#authModal:not(.hidden)", timeout=10000)
            await pg.fill("#auName", "Teste"); await pg.fill("#auEmail", email); await pg.fill("#auPw", "segredo123")
            try:
                async with pg.expect_download(timeout=120000) as dl: await pg.click("#auBtn")
                ok(name + " download", (await dl.value).suggested_filename)
            except Exception as e: ok(name + " download", "FALHOU " + str(e)[:80])
            await pg.wait_for_timeout(1000)
            for h in ["#projetos", "#creator", "#oportunidades", "#nuvem", "#conta", "#criar"]:
                await pg.goto(B + "/" + h); await pg.wait_for_timeout(1300)
                vis = await pg.evaluate("[...document.querySelectorAll('main.view,section.view,.view')].filter(e=>!e.classList.contains('hidden')&&e.offsetParent!==null).map(e=>e.id).join(',')")
                ok(f"{name} {h}", vis + f" | overflow {await overflow(pg)}")
                await pg.screenshot(path=f"/tmp/full_{name}_{h[1:]}.png")
            ok(name + " erros", errs[:10])
            await ctx.close()
        # admin com nível máximo
        ctx = await br.new_context(viewport={"width": 1300, "height": 900}); pg = await ctx.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        await pg.goto(B + "/"); await pg.wait_for_timeout(800)
        r = await pg.evaluate("""async()=>{const s=await fetch('/api/auth/signup',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:'eusene1212@gmail.com',password:'segredo123',name:'Pedro'})});const c=await fetch('/api/creator/me',{credentials:'include'}).then(r=>r.json());const a=await fetch('/api/creator/avatar',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({avatar_id:'dragon'})});const f=await fetch('/api/admin/funnel?days=1',{credentials:'include'});return {signup:s.status,level:c.level,title:c.title,dragon:a.status,funnel:f.status}}""")
        ok("eusene1212 nível/admin", r)
        await pg.goto(B + "/?r=1#metricas"); await pg.wait_for_timeout(3500); ok("admin métricas visível", await pg.is_visible("#fun2"))
        await pg.goto(B + "/#creator"); await pg.wait_for_timeout(1500); await pg.screenshot(path="/tmp/full_creator_max.png")
        ok("admin erros", errs[:5])
        await br.close()
    print(json.dumps(R, indent=1, ensure_ascii=False))
asyncio.run(main())
