# probe_site.py
# pip install requests beautifulsoup4
# (옵션) pip install playwright && playwright install

import re, sys, json, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlsplit

UA = {"User-Agent": "Mozilla/5.0 (probe) drspark/monitor"}

def probe_http(url):
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    return r

def find_rss_links(soup, base):
    out = []
    for link in soup.select("link[rel='alternate'][type*='rss'], link[rel='alternate'][type*='atom']"):
        href = link.get("href")
        if href:
            out.append(urljoin(base, href))
    # 관례 경로 추정
    for path in ["/rss", "/feed", "/feed.xml", "/atom.xml", "/feeds", "/rss.xml"]:
        out.append(urljoin(base, path))
    return list(dict.fromkeys(out))  # uniq

def find_json_patterns(text):
    # 너무 일반적이지만 단서가 되는 패턴들
    pats = [
        r"https?://[^\s\"']+/api/[^\s\"']+",
        r"https?://[^\s\"']+/graphql",
        r"/api/[A-Za-z0-9_\-/?&=%]+",
        r"/graphql",
        r"/posts\?[^\s\"']+",
        r"/notices\?[^\s\"']+",
    ]
    urls = []
    for p in pats:
        for m in re.findall(p, text):
            urls.append(m)
    return list(dict.fromkeys(urls))

def quick_html_extract(soup):
    # “공지”라는 단어가 들어간 링크/제목을 대강 추출
    candidates = []
    for a in soup.find_all("a"):
        t = (a.get_text(strip=True) or "")
        if not t:
            continue
        if any(kw in t for kw in ["공지", "Notice", "뉴스", "알림"]):
            candidates.append(t[:120])
    return candidates[:10]

def main(url):
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"

    print(f"[+] GET {url}")
    r = probe_http(url)
    soup = BeautifulSoup(r.text, "html.parser")

    print("\n== RSS/Atom 후보 ==")
    for u in find_rss_links(soup, base):
        print("  -", u)

    print("\n== 초기 HTML에서 '공지' 텍스트 후보 ==")
    for t in quick_html_extract(soup):
        print("  -", t)

    print("\n== 스크립트/HTML에서 API URL 패턴 추정 ==")
    dump = r.text
    # 외부 스크립트도 조금 긁어온다(상위 5개만)
    for i, s in enumerate(soup.select("script[src]")[:5]):
        src = urljoin(base, s.get("src"))
        try:
            js = probe_http(src).text
            dump += "\n" + js[:200000]  # 과한 크기 방지
            print(f"  [script] {src} (ok)")
        except Exception as e:
            print(f"  [script] {src} (fail: {e})")

    apis = find_json_patterns(dump)
    for u in apis[:20]:
        print("  -", urljoin(base, u) if u.startswith("/") else u)

    print("\n== 정적/동적 구분 힌트 ==")
    # 아주 러프: 초기 HTML에서 공지 후보가 충분히 보이면 정적 가능성↑
    if len(quick_html_extract(soup)) >= 2:
        print("  * 초기 HTML에 공지 텍스트가 다수 보임 → 정적 HTML일 가능성 높음.")
    else:
        print("  * 초기 HTML에 공지 텍스트가 적음 → JS로 채우는 동적 가능성. Network/XHR 확인 권장.")

if __name__ == "__main__":
    # if len(sys.argv) < 2:
    #     print("usage: python probe_site.py <URL>")
    #     sys.exit(1)
    # main(sys.argv[1])
    main("https://www.ajou.ac.kr/kr/ajou/notice.do")
