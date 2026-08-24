"""Verify whether the two failed vids' download URLs are still valid
or have expired (iwara URL hashes expire in 24h).

We ask the HK crawler for the full current 30 items, then run each
URL through a HEAD request with timeout. This is read-only and never
touches the US bot state.
"""
import asyncio
import json
import os

os.chdir("/home/a27exe/Projects/code/project/videoAcquisition-hk")

from camoufox import AsyncCamoufox
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path

URL = "https://www.iwara.tv/videos?sort=trending&page=1"
STATE = json.load(open("config/auth/iwara_auth.json"))
ADDON = get_addon_path()

# Original failed vids from the US bot run
FAILED_VIDS = {
    "MDD_Ako": "jPXcuAj9o7jdEZ",
    "Shinano": "YqDVjMGbAwtkLJ",
}


async def main():
    async with AsyncCamoufox(
        headless=True, geoip=True, humanize=True, i_know_what_im_doing=True,
        config={"forceScopeAccess": True}, disable_coop=True, main_world_eval=True,
        proxy=None, addons=[os.path.abspath(ADDON)],
    ) as browser:
        ctx = await browser.new_context(storage_state=STATE)

        # Step 1: Re-fetch the current trending list to find what rank each
        # vid is at now.
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        for s in (0, 5, 15):
            if s: await page.wait_for_timeout((5 if s == 5 else 10) * 1000)
            cards = await page.locator(".page-videoList__item").count()
            if cards >= 20: break

        # Pull the current trending IDs via DOM (same as fuck_cf)
        hrefs = await page.evaluate("""() => Array.from(document.querySelectorAll('a.videoTeaser__thumbnail')).map(a => a.getAttribute('href'))""")
        ids = [h.split("/video/")[1].split("/")[0].split("?")[0] for h in hrefs if h]
        print(f"Current top {len(ids)} IDs: {ids[:5]}...{ids[-3:]}")
        for k, v in FAILED_VIDS.items():
            print(f"  {k} ({v}):", "rank", ids.index(v) + 1 if v in ids else "NOT in current top 30")
        await page.close()

        # Step 2: For each vid that IS in current top, ask iwara API directly,
        # get download_url, then test it with HEAD.
        for k, vid in FAILED_VIDS.items():
            print(f"\n=== {k} ({vid}) ===")
            api_page = await ctx.new_page()
            try:
                resp = await api_page.goto(
                    f"https://apiq.iwara.tv/video/{vid}",
                    wait_until="domcontentloaded", timeout=60000,
                )
                # apiq returns JSON wrapped in <pre>...</pre>
                api_text = await api_page.content()
                print(f"  apiq status={resp.status if resp else None} bytes={len(api_text)}")
                # extract the JSON inside <pre>
                import re
                m = re.search(r"<pre>(.*?)</pre>", api_text, re.DOTALL)
                if not m:
                    print("  no <pre> JSON, raw first 200:", api_text[:200])
                    continue
                api_data = json.loads(m.group(1))
                file_url = api_data.get("fileUrl", "")
                file_id = str(api_data.get("file", {}).get("id", ""))
                print(f"  file_url={file_url[:80]}")
                print(f"  file_id={file_id}")
                if not file_url or not file_id:
                    print("  missing fileUrl/file_id")
                    continue

                # Step 3: HEAD request on the fileUrl
                import hashlib, urllib.request
                m2 = re.search(r"[?&]expires=(\d+)", file_url)
                if not m2:
                    print("  no expires in fileUrl")
                    continue
                expires = m2.group(1)
                suffix = "_mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
                t_hash = hashlib.sha1((file_id + "_" + expires + suffix).encode()).hexdigest()
                # follow redirects, ignore cert
                req = urllib.request.Request(file_url, method="HEAD",
                                              headers={"X-Version": t_hash, "User-Agent": "curl/8"})
                try:
                    r = urllib.request.urlopen(req, timeout=15)
                    print(f"  HEAD status={r.status} content-length={r.headers.get('Content-Length','?')}")
                except urllib.error.HTTPError as e:
                    print(f"  HEAD HTTPError code={e.code} reason={e.reason}")
                    body = e.read(200).decode("utf-8", errors="replace")
                    print(f"  body first 200: {body[:200]}")
                except Exception as e:
                    print(f"  HEAD error: {type(e).__name__}: {e}")
            finally:
                await api_page.close()
        await ctx.close()


asyncio.run(main())