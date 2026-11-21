import asyncio, json
from playwright.async_api import async_playwright
async def run(pg, f):
    await pg.goto("http://localhost:8765/#criar"); await pg.wait_for_timeout(1200)
    await pg.evaluate("localStorage.removeItem('nc_guest_done')")
    await pg.set_input_files("#file", f); await pg.wait_for_timeout(800)
    await pg.click("#go"); await pg.wait_for_selector("#results:not(.hidden)", timeout=180000)
    await pg.wait_for_timeout(1500)
    r = await pg.evaluate("(()=>{const S=window.__nc.state();return JSON.stringify({cam:S.camAuto,n:S.clips.length,tpl:S.clips[0]&&S.clips[0].tpl})})()")
    await pg.click("#grid .clip >> nth=0"); await pg.wait_for_timeout(2500)
    await pg.screenshot(path="/tmp/ed_"+f.split('/')[-1]+".png")
    return r
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox","--autoplay-policy=no-user-gesture-required"])
        for f in ["/tmp/fc_game.mp4","/tmp/fc_cam.mp4","/tmp/fc_nocam.mp4","/tmp/fc_drive.mp4"]:
            ctx = await b.new_context(viewport={"width":1280,"height":900}); pg = await ctx.new_page(); errs=[]
            pg.on("pageerror", lambda e: errs.append(str(e)))
            print(f, await run(pg, f), errs[:3]); await ctx.close()
        await b.close()
asyncio.run(main())
