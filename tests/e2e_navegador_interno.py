import asyncio
from playwright.async_api import async_playwright
UA="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0 Mobile Safari/537.36 Instagram 310.0.0.0 Android"
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        d = dict(p.devices["Pixel 7"]); d["user_agent"]=UA
        ctx = await b.new_context(**d); pg = await ctx.new_page(); errs=[]
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("http://localhost:8765/?ref=instagram"); await pg.wait_for_timeout(1500)
        print("banner:", await pg.is_visible("[data-inapp]"), (await pg.inner_text("[data-inapp]"))[:120])
        await pg.screenshot(path="/tmp/inapp.png")
        href = await pg.evaluate("chromeIntent()") if False else None
        await pg.click("[data-demo-run]"); await pg.wait_for_selector("#results:not(.hidden)", timeout=60000)
        await pg.click("[data-dl='0']"); await pg.fill("#auName","x"); await pg.fill("#auEmail","ig%d@t.com"%int(__import__('time').time())); await pg.fill("#auPw","segredo123"); await pg.click("#auBtn")
        await pg.wait_for_selector("#svSave", timeout=90000)
        print("save card:", (await pg.inner_text("#svSave"))[:200].replace("\n"," | "))
        print("errs", errs)
        await b.close()
asyncio.run(main())
