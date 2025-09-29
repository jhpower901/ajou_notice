# requirements:
#   pip install requests beautifulsoup4 apscheduler

import os, re, time, sqlite3, logging, sys
from logging.handlers import RotatingFileHandler, SysLogHandler, NTEventLogHandler

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

from apscheduler.schedulers.blocking import BlockingScheduler


# ==================== 환경변수 ====================
BASE       = os.getenv("AJOU_BASE_URL", "https://www.ajou.ac.kr")
LIST_URL   = os.getenv("AJOU_LIST_URL", "https://www.ajou.ac.kr/kr/ajou/notice.do")
DB_PATH    = os.getenv("AJOU_DB_PATH",  os.path.join(os.getcwd(), "ajou_seen.db"))
#WEBHOOK    = os.getenv("DISCORD_WEBHOOK_AJOU_NOTICE_URL", "")
WEBHOOK    = os.getenv("DISCORD_WEBHOOK_URL", "")
INTERVAL_S = int(os.getenv("AJOU_INTERVAL_SECONDS", "60"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AjouNoticeBot/1.0; +notice-monitor)"
}

# ==================== 로깅 설정 ====================
logger = logging.getLogger("ajou_notice")

def setup_logging():
    logger.setLevel(logging.INFO)

    # 콘솔
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    # 회전 파일 (5MB x 5개)
    fh = RotatingFileHandler("ajou_notice.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch.setFormatter(fmt); fh.setFormatter(fmt)
    logger.addHandler(ch); logger.addHandler(fh)

    # (선택) 시스템 로그
    try:
        if os.name == "nt":
            eh = NTEventLogHandler(appname="AjouNoticeWatcher")
            eh.setLevel(logging.WARNING)
            eh.setFormatter(fmt)
            logger.addHandler(eh)
        else:
            # journald가 /dev/log를 안 쓸 수도 있으니 try
            sh = SysLogHandler(address="/dev/log")
            sh.setLevel(logging.WARNING)
            sh.setFormatter(logging.Formatter("AjouNotice: %(levelname)s %(message)s"))
            logger.addHandler(sh)
    except Exception:
        logger.debug("System log handler not attached", exc_info=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 이슈 완화
except Exception:
    pass

setup_logging()


# ==================== requests 세션 ====================
_session = None
def get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.6,  # 0.6s, 1.2s, 1.8s...
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session

def fetch_html(url: str = LIST_URL) -> str:
    ses = get_session()
    logger.info(f"GET {url}")
    r = ses.get(url, headers=HEADERS, timeout=15)
    if r.status_code >= 400:
        snippet = (r.text or "")[:500].replace("\n", " ")
        logger.warning(f"HTTP {r.status_code} for {url} | body[:500]={snippet}")
        r.raise_for_status()
    return r.text


# ==================== DB (새 글 판별) ====================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen(
            post_id TEXT PRIMARY KEY,
            first_seen_ts INTEGER
        )
    """)
    con.commit(); con.close()

def is_new(pid: str) -> bool:
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE post_id=?", (pid,))
    known = cur.fetchone() is not None
    if not known:
        cur.execute("INSERT INTO seen(post_id, first_seen_ts) VALUES(?,?)", (pid, int(time.time())))
        con.commit()
    con.close()
    return not known


# ==================== 파싱 유틸/함수 ====================
ARTICLENO_RE = re.compile(r"(?:articleNo=|fnView\()(\d+)")
DIGITS_RE    = re.compile(r"\d+")
IMG_EXT      = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

def _get_text(el):
    return el.get_text(" ", strip=True) if el else ""

def _parse_views(text: str) -> int | None:
    m = DIGITS_RE.search(text or "")
    return int(m.group()) if m else None

def parse_ajou_notice_list(html: str) -> list[dict]:
    """
    아주대 공지 목록 파서
    - 상단 고정공지/일반공지 모두 커버
    - 각 행(tr)에서 .b-title-box a[href] 기준
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for tr in soup.select("tr"):
        a = tr.select_one(".b-title-box a[href]")
        if not a:
            continue

        # 핀 여부
        cls = tr.get("class", [])
        pinned = (
            "b-top-box" in cls
            or tr.select_one(".b-num-box.num-notice, .b-title-box .b-notice") is not None
            or (_get_text(tr.select_one(".b-num-box")) == "공지")
        )

        title = _get_text(a)

        # href가 '?mode=view&...' 형태라서 반드시 목록 URL 기준으로 결합해야 함!
        href = a.get("href", "")
        url  = urljoin(LIST_URL, href)

        # 고유 ID
        qs = parse_qs(urlparse(url).query)
        art_no = (qs.get("articleNo") or [""])[0]
        if not art_no:
            m = re.search(r"articleNo=(\d+)", href)
            if not m:
                continue
            art_no = m.group(1)

        # 메타
        tds = tr.find_all("td")
        category = _get_text(tr.select_one(".b-m-con .b-cate"))
        writer   = _get_text(tr.select_one(".b-m-con .b-writer"))
        date     = _get_text(tr.select_one(".b-m-con .b-date"))

        if not category and len(tds) >= 2: category = _get_text(tds[1])
        if not writer   and len(tds) >= 5: writer   = _get_text(tds[-2])
        if not date     and len(tds) >= 6: date     = _get_text(tds[-1])

        is_new  = tr.select_one(".b-etc-box .b-new span") is not None
        views   = _parse_views(_get_text(tr.select_one(".b-m-con .hit")))
        has_files = (
            tr.select_one(".b-common-file-box") is not None
            or "첨부파일" in _get_text(tr.select_one(".b-m-con .b-file"))
        )

        items.append({
            "id": art_no,
            "url": url,
            "title": title,
            "category": category,
            "writer": writer,
            "date": date,         # 예: 25.09.29 또는 2025-09-29
            "views": views,       # int | None
            "is_new": is_new,
            "pinned": pinned,
            "has_files": has_files,
            "thumb": None,        # 이후 상세에서 채움
        })

    logger.info(f"Parsed {len(items)} rows")
    return items

def _abs(u: str) -> str:
    if not u: return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    return urljoin(BASE, u)

def parse_detail_first_image(html: str) -> str | None:
    """
    상세 HTML에서 본문 이미지 중 가장 적합한 한 장을 골라 절대URL 반환.
    우선순위: #cms-content .fr-view img -> .b-content-box img -> og:image
    """
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    for img in soup.select("#cms-content .fr-view img, .b-content-box img"):
        src = (img.get("src") or img.get("data-src") or img.get("data-path") or "").strip()
        if not src:
            continue
        low = src.lower()
        if not low.endswith(IMG_EXT):
            fname = (img.get("data-file_name") or "").lower()
            if not fname.endswith(IMG_EXT):
                continue

        # 너무 작은 UI 아이콘류 제외
        if re.search(r"(icon|ico-|btn-|sprite|arrow|logo)", low) and "/editor-image/" not in low:
            continue

        score = 0
        try: score += int(img.get("data-width") or 0)
        except: pass
        try: score += int(img.get("data-size") or 0) // 1024
        except: pass
        if "width" in (img.get("style") or ""): score += 300
        if "/editor-image/" in low: score += 500

        candidates.append((score, src))

    if candidates:
        candidates.sort(reverse=True)
        return _abs(candidates[0][1])

    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return _abs(og["content"])

    return None

def fetch_detail_thumb(detail_url: str) -> str | None:
    try:
        html = fetch_html(detail_url)
        thumb = parse_detail_first_image(html)
        if thumb:
            logger.info(f"[thumb] {detail_url} -> {thumb}")
        else:
            logger.info(f"[thumb] no image in content: {detail_url}")
        return thumb
    except Exception as e:
        logger.exception(f"[thumb] fetch failed for {detail_url}: {e}")
        return None


# ==================== Discord 전송 ====================
def discord_send(item: dict):
    """
    Discord 웹훅으로 임베드 전송 (레이트리밋 대응 간단 재시도)
    """
    if not WEBHOOK:
        logger.warning("DISCORD_WEBHOOK_URL이 비어 있습니다. 환경변수로 설정해주세요.")
        return

    title = (item.get("title") or "").strip()
    url   = item.get("url") or ""
    category = item.get("category") or "—"
    writer   = item.get("writer") or "—"
    date     = item.get("date") or "—"
    pinned   = " (고정)" if item.get("pinned") else ""
    new_badge= "NEW · " if item.get("is_new") else ""
    views    = item.get("views")

    embed = {
        "title": title + pinned,
        "url": url,
        "color": 0x2E86DE,  # 임의 색상
        "fields": [
            {"name": "분류", "value": category, "inline": True},
            {"name": "작성부서", "value": writer, "inline": True},
            {"name": "작성일", "value": date, "inline": True},
        ],
        "footer": {"text": f"{new_badge}조회수 {views}" if views is not None else f"{new_badge}"},
    }
    if item.get("thumb"):
        embed["thumbnail"] = {"url": item["thumb"]}

    payload = {"content": None, "embeds": [embed]}

    max_attempts = 3
    for attempt in range(1, max_attempts+1):
        try:
            logger.info(f"[Discord] send {attempt}/{max_attempts} | '{title}'")
            r = requests.post(WEBHOOK, json=payload, timeout=10)
            if r.status_code == 204 or (200 <= r.status_code < 300):
                logger.info(f"[Discord] sent OK ({r.status_code}): '{title}'")
                return
            if r.status_code == 429:
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                retry_after = float(data.get("retry_after", 1.0))
                logger.warning(f"[Discord] 429 rate limited: retry_after={retry_after}s")
                time.sleep(retry_after + 0.25)
                continue
            snippet = (r.text or "")[:300].replace("\n", " ")
            logger.error(f"[Discord] HTTP {r.status_code}: body[:300]={snippet}")
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.exception(f"[Discord] POST failed attempt {attempt}: {e}")
            if attempt < max_attempts:
                time.sleep(1.2 * attempt)
                continue
            raise


# ==================== 주기 실행 ====================
def run_once():
    try:
        html = fetch_html(LIST_URL)
        for it in parse_ajou_notice_list(html):
            # if it.get("pinned"):  # 상단 고정은 스킵
            #     logger.debug(f"Skip pinned: {it['id']}")
            #     continue
            if is_new(it["id"]):
                it["thumb"] = fetch_detail_thumb(it["url"])
                logger.info(f"NEW: {it['title']} -> {it['url']} thumb={it['thumb'] or '-'}")
                discord_send(it)
            else:
                logger.debug(f"Seen: {it['id']}")
    except Exception as e:
        logger.exception(f"run_once failed: {e}")


# ==================== 진입점 ====================
if __name__ == "__main__":
    init_db()
    # --once 옵션이면 한 번만 실행
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        run_once()
        sys.exit(0)

    # 아니면 스케줄러
    scheduler = BlockingScheduler()
    # misfire_grace_time: 지연되어도 60초 이내면 보정
    scheduler.add_job(run_once, "interval",
                      seconds=INTERVAL_S,
                      jitter=min(20, INTERVAL_S // 2),
                      max_instances=1,
                      coalesce=True,
                      misfire_grace_time=60)
    logger.info(f"Watcher started: every {INTERVAL_S}s | LIST_URL={LIST_URL} | DB={DB_PATH}")
    scheduler.start()
