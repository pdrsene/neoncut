import asyncio, json, time
from playwright.async_api import async_playwright
BASE="http://localhost:8765"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox","--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(viewport={"width":1280,"height":900}, accept_downloads=True); pg = await ctx.new_page(); errs=[]
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE+"/?ref=teste"); await pg.wait_for_timeout(1200)
        await pg.set_input_files("#hxFileIn", "/tmp/meu_video.mp4")
        await pg.wait_for_timeout(1200)
        await pg.screenshot(path="/tmp/create_quick.png")
        r = {"go_enabled": await pg.is_enabled("#go"), "quick": await pg.evaluate("$('#upload').classList.contains('quick')") if False else await pg.evaluate("document.querySelector('#upload').classList.contains('quick')")}
        await pg.click("#go")
        await pg.wait_for_selector("#results:not(.hidden)", timeout=120000)
        r["clips"] = await pg.locator("#grid .clip").count()
        r["ffree_visible"] = await pg.is_visible("#ffreeNote")
        await pg.click("[data-dl='0']"); await pg.wait_for_selector("#authModal:not(.hidden)")
        await pg.fill("#auName","Dono"); await pg.fill("#auEmail", f"dono{int(time.time())}@teste.com"); await pg.fill("#auPw","segredo123")
        async with pg.expect_download(timeout=120000) as dl: await pg.click("#auBtn")
        r["file1"] = (await dl.value).suggested_filename
        await pg.wait_for_timeout(1500)
        r["ffree_after"] = await pg.is_visible("#ffreeNote")
        async with pg.expect_download(timeout=120000) as dl: await pg.click("[data-dl='0']")
        r["file2"] = (await dl.value).suggested_filename
        r["errs"]=errs
        print(json.dumps(r, indent=1))
        await b.close()
asyncio.run(main())
