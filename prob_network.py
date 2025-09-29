# probe_network.py
# pip install playwright && playwright install
from playwright.sync_api import sync_playwright
import sys, json

def main(url):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context()
        page = ctx.new_page()

        records = []
        def on_req(req):
            if req.resource_type in ("xhr", "fetch"):
                records.append({"method": req.method, "url": req.url})
        def on_res(res):
            try:
                ct = res.headers.get("content-type","")
                if any(x in ct for x in ["json","xml","rss","atom"]):
                    body = res.text()[:800]
                else:
                    body = ""
                records.append({"response": True, "url": res.url, "status": res.status, "content_type": ct, "body_sample": body})
            except Exception:
                pass

        page.on("request", on_req)
        page.on("response", on_res)

        page.goto(url, wait_until="networkidle", timeout=30000)
        print(json.dumps(records, ensure_ascii=False, indent=2))
        b.close()

if __name__ == "__main__":
    # if len(sys.argv) < 2:
    #     print("usage: python probe_network.py <URL>")
    #     sys.exit(1)
    # main(sys.argv[1])
    main("https://www.ajou.ac.kr/kr/ajou/notice.do")

