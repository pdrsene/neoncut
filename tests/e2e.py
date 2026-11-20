# Teste ponta a ponta: landing, demonstração (pipeline real), cadastro, 1º corte grátis e download.
import asyncio, json, sys, time
from playwright.async_api import async_playwright
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--autoplay-policy=no-user-gesture-required", "--no-sandbox"])
        res = {}
        for name, opts in [("desktop", {"viewport": {"width": 1366, "height": 860}}), ("mobile", {**p.devices["Pixel 7"]})]:
            ctx = await b.new_context(**opts, accept_downloads=True)
            pg = await ctx.new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
            pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
            await pg.goto(BASE + "/?utm_source=teste&utm_campaign=kit")
            await pg.wait_for_timeout(2500)
            await pg.screenshot(path=f"/tmp/{name}_landing.png", full_page=False)
            await pg.screenshot(path=f"/tmp/{name}_landing_full.png", full_page=True)
            # demonstração
            await pg.click("[data-demo-run]")
            t0 = time.time()
            await pg.wait_for_selector("#results:not(.hidden)", timeout=120000)
            res[name + "_demo_s"] = round(time.time() - t0, 1)
            await pg.wait_for_timeout(1500)
            res[name + "_clips"] = await pg.locator("#grid .clip").count()
            res[name + "_sampletag"] = await pg.is_visible("#sampleTag2")
            await pg.screenshot(path=f"/tmp/{name}_results.png")
            # baixar pede cadastro
            await pg.click("[data-dl='0']")
            await pg.wait_for_selector("#authModal:not(.hidden)")
            email = f"t{int(time.time()*1000)}_{name}@teste.com"
            await pg.fill("#auName", "Teste"); await pg.fill("#auEmail", email); await pg.fill("#auPw", "segredo123")
            async with pg.expect_download(timeout=120000) as dl:
                await pg.click("#auBtn")
            d = await dl.value
            res[name + "_download"] = d.suggested_filename
            await pg.wait_for_timeout(800)
            await pg.screenshot(path=f"/tmp/{name}_after_dl.png")
            res[name + "_errors"] = [e for e in errs if "favicon" not in e][:8]
            await ctx.close()
        print(json.dumps(res, indent=1, ensure_ascii=False))
        await b.close()
asyncio.run(main())
