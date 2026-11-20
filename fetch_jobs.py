#!/usr/bin/env python3
"""
从 Jobsdb HK 搜索 URL 抓取所有岗位列表（SEEK v5 JSON API 版）。

用法:
    python3 fetch_jobs.py "<搜索URL>" [-o output.json] [--max-pages N] [--page-size N]
    python3 fetch_jobs.py --keyword ESG --classification-id 6076 --where "Hong Kong" -o esg.json
    python3 fetch_jobs.py "<搜索URL>" -o out.json --resume   # 从上次中断处续爬
    python3 fetch_jobs.py "<搜索URL>" -o out.json --with-jd  # 列表 + JD 一条命令完成
    python3 fetch_jobs.py --jd-input new_unique.json --jd-output jd.json
                                                             # 只对已有岗位文件抓 JD（替代 fetch_jd.py）

URL 示例:
    https://hk.jobsdb.com/zh/ESG-jobs
    https://hk.jobsdb.com/zh/quality-engineer-jobs

说明（与旧版的区别）:
    旧版用 Playwright 预热 + curl_cffi 爬 /zh/{keyword}-jobs 的 HTML（抽
    SEEK_REDUX_DATA），那一层正是 Cloudflare 守得最紧的，而且旧版从不检查
    HTTP 状态码、只认 "Just a moment" 字样，真正的 403 硬封锁页
    （"Attention Required / Sorry, you have been blocked"）会被误判为
    「找不到数据」而静默结束——这就是爬到第 4 页左右停掉的原因。
    本版改为直接调用网站前端自己用的 JSON API:
        GET https://hk.jobsdb.com/api/jobsearch/v5/search
    并具备: 显式状态码判断、封锁页识别、指数退避重试、逐页落盘（中断不丢
    已抓数据）、--resume 断点续爬。

    传输模式 --mode:
      api     —— curl_cffi 直连 API（快，但某些网络下直连会被 Cloudflare 拦）
      browser —— 真实 Chrome 内用 fetch() 打 API（某些环境下连页面内 fetch
                 和地址栏直开 /api/ 都被拦，见 nav）
      nav     —— 导航收割：真实 Chrome 逐页打开搜索页，抽页面内嵌的
                 SEEK_REDUX_DATA（结果由服务器渲染进 HTML）。不自己发任何
                 API 请求，是目前对该用户环境实测唯一走得通的路径。
                 翻页优先撳页面上的分页链接（走网站自己的流程）；途中若出现
                 Turnstile 互动验证（「验证您是人类」勾选框），程序先自动
                 勾选一次，失败才停下来等用户手动点（最长 180 秒），
                 通过后自动继续，不消耗重试次数
      auto    —— 默认。依次探测 api → browser → nav，哪个通用哪个
    浏览器类模式首次运行会弹出 Chrome 窗口，若出现 Cloudflare 验证页，
    在窗口里手动完成即可，profile 会记住，之后一般无需再处理。
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse

# ── HTTP 客户端：优先 curl_cffi（浏览器 TLS 指纹），缺失时退回标准库 ──────
try:
    from curl_cffi import requests as _cffi_requests

    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

API_URL = "https://hk.jobsdb.com/api/jobsearch/v5/search"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 每次被封锁/限流后的等待秒数（共重试 len(RETRY_WAITS) 次）
RETRY_WAITS = (10, 30, 90)

# Cloudflare 拦截页特征（硬封锁页不含 "Just a moment"，必须一并识别）
BLOCK_MARKERS = (
    "Just a moment",
    "cf-chl",
    "Attention Required",
    "Sorry, you have been blocked",
    "challenges.cloudflare.com",
)

# ── Classification slug → ID mapping (Jobsdb HK，沿用旧版) ───────────────
CLASSIFICATION_MAP = {
    "marketing-communications": "6008",
    "sales": "6004",
    "information-technology": "6014",
    "accounting": "6001",
    "admin-human-resources": "6002",
    "banking-financial-services": "6003",
    "building-construction": "6005",
    "design-architecture": "6006",
    "education-training": "6007",
    "engineering": "6009",
    "fashion": "6019",
    "healthcare": "6010",
    "hospitality-f-b": "6011",
    "insurance": "6012",
    "legal": "6016",
    "management": "6013",
    "manufacturing": "6015",
    "media-advertising": "6017",
    "property-real-estate": "6018",
    "public-sector": "6020",
    "sciences-research": "6021",
    "supply-chain": "6024",
    "transport-logistics": "6022",
    "trades-services": "6023",
}

WORKTYPE_MAP = {
    "full-time": "242",
    "part-time": "243",
    "contract": "244",
    "temporary": "245",
    "casual": "246",
    "freelance": "250",
    "internship": "248",
    "graduate": "247",
}

WORK_ARRANGEMENT_MAP = {
    "on-site": "1",
    "remote": "2",
    "hybrid": "3",
}

DEFAULT_PAGE_SIZE = 30

# 搜索 URL query 中可直接透传给 API 的参数
_QUERY_PASSTHROUGH = (
    "keywords", "where", "classification", "subclassification",
    "worktype", "workarrangement", "salaryrange", "salarytype",
    "daterange", "sortmode", "pageSize",
)


class BlockedError(Exception):
    """被 Cloudflare 封锁/限流，重试后仍失败。"""


class FetchError(Exception):
    """其他不可重试的请求错误。"""


# ── HTTP 层 ──────────────────────────────────────────────────────────────

class _ApiClient:
    """curl_cffi 直连 API，get(params) 返回 (status_code, text)。"""

    def __init__(self):
        self._session = None
        self._opener = None
        if _HAS_CFFI:
            self._session = _cffi_requests.Session(impersonate="chrome124")
            self._session.headers["User-Agent"] = _UA
        else:
            import http.cookiejar
            import urllib.request

            cj = http.cookiejar.CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj)
            )
            print("[WARN] 未安装 curl_cffi，退回标准库请求（建议 pip install curl_cffi）",
                  file=sys.stderr)

    def get(self, params: dict, timeout: int = 30):
        if self._session is not None:
            resp = self._session.get(API_URL, params=params, timeout=timeout)
            return resp.status_code, resp.text

        import urllib.error
        import urllib.request

        url = API_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


class _BrowserBase:
    """真实 Chrome + 持久化 profile 的公共基类（启动、等验证、关闭）。

    与旧版「预热后把 cookie 搬去 curl_cffi」不同：一切请求都留在浏览器内
    发出，cookie / TLS 指纹 / 浏览器信任都是原生的，不存在搬运失效问题。
    """

    def __init__(self, start_url: str, profile_dir: str, headless: bool = False):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise FetchError(
                "browser 模式需要 playwright：请先运行 "
                "pip install playwright 且确保本机装有 Google Chrome"
            )
        self._pw = sync_playwright().start()
        self._context = None
        last_err = None
        # 优先用本机真实 Chrome，指纹与日常浏览器一致；失败再退回 Chromium
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    **kwargs,
                )
                break
            except Exception as e:
                last_err = e
        if self._context is None:
            self._pw.stop()
            raise FetchError(
                f"无法启动浏览器（{last_err}）。若退回的 Chromium 未安装，"
                "请运行: python -m playwright install chromium"
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        print(f"[INFO] 浏览器模式：打开 {start_url} ...", file=sys.stderr)
        try:
            self._page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[WARN] 打开页面异常: {e}", file=sys.stderr)
        if not self._wait_clear(90):
            print("[WARN] Cloudflare 验证 90 秒内未通过。若窗口里有验证页，"
                  "请手动完成后重新运行（profile 已保存进度，下次会更快通过）。",
                  file=sys.stderr)

    def _wait_clear(self, seconds: int) -> bool:
        """等 Cloudflare challenge 消失（有头模式下用户可手动完成验证）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                title = self._page.title()
            except Exception:
                title = ""
            if "Just a moment" not in title:
                return True
            time.sleep(2)
        return False

    def close(self):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._pw.stop()


class _BrowserClient(_BrowserBase):
    """在 JobsDB 页面内用 fetch() 打 API。get(params) 与 _ApiClient 一致。"""

    def get(self, params: dict, timeout: int = 30):
        if "Just a moment" in (self._page.title() or ""):
            self._wait_clear(60)
        url = API_URL + "?" + urllib.parse.urlencode(params)
        result = self._page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {"Accept": "application/json"}});
                const t = await r.text();
                return {status: r.status, text: t};
            }""",
            url,
        )
        return result["status"], result["text"]


def _extract_redux_json(html: str):
    """从 HTML 中截取 window.SEEK_REDUX_DATA 的完整 JSON（括号配平，沿用旧版）。"""
    i = html.find("window.SEEK_REDUX_DATA")
    if i < 0:
        return None
    s = html.find("{", i)
    if s < 0:
        return None
    depth = 0
    end = -1
    instr = False
    esc = False
    for j in range(s, len(html)):
        c = html[j]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            instr = not instr
            continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(html[s:end])
    except json.JSONDecodeError:
        return None


# ── JD 提取（沿用原 fetch_jd.py 的已验证逻辑）────────────────────────────

def clean_html_content(text: str) -> str:
    """把 JD 的 HTML 内容清洗为纯文本（与原 fetch_jd.py 完全一致）。"""
    if not text:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    block_tags = ['<br', '</p>', '</div>', '</li>', '</h1>', '</h2>', '</h3>',
                  '</h4>', '</h5>', '</h6>', '</tr>', '</section>', '</article>']
    for tag in block_tags:
        text = text.replace(tag, '\n')
    inline_tags = ['</td>', '</span>', '</a>', '</strong>', '</b>', '</i>', '</em>']
    for tag in inline_tags:
        text = text.replace(tag, ' ')
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'^\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_jd_from_html(html: str) -> str:
    """从详情页 HTML 的 <script> JSON 中提取 JD 全文（与原 fetch_jd.py 一致）。"""
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    candidates = []
    for s in scripts:
        if len(s) < 3000:
            continue
        contents = re.findall(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        for c in contents:
            if len(c) > 200:
                candidates.append(c)
        content_match = re.search(r'"content"\s*:\s*"(.+?)"(?:\s*[,}])', s)
        if content_match:
            c = content_match.group(1)
            if len(c) > 200 and c not in candidates:
                candidates.append(c)
    if not candidates:
        return ""
    filtered = [c for c in candidates if "Career Advice" not in c]
    if filtered:
        candidates = filtered
    best = max(candidates, key=len)
    decoded = best
    for a, b in (('\\u003C', '<'), ('\\u003E', '>'), ('\\u002F', '/'),
                 ('\\u0026', '&'), ('\\u003c', '<'), ('\\u003e', '>'),
                 ('\\u002f', '/'), ('\\"', '"'), ('\\n', '\n'),
                 ('\\t', '\t'), ('\\\\', '\\'),
                 ('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                 ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"),
                 ('&apos;', "'")):
        decoded = decoded.replace(a, b)
    return clean_html_content(decoded)


class _BrowserNavClient(_BrowserBase):
    """导航收割模式：浏览器逐页打开搜索页，抽页面内嵌的 SEEK_REDUX_DATA。

    实测（2026-11-20）：在该用户环境下，/api/ 路径连浏览器地址栏直开都被
    Cloudflare 拦，但搜索页 HTML 导航可正常打开（结果由服务器渲染进页面）。
    本模式不自己发任何 API 请求，只读浏览器已渲染页面里的数据。
    get(params) 只取 params["page"]，返回合成的 API 形状 JSON 文本。
    """

    # 互动验证页（Turnstile「验证您是人类」）特征，与一般封锁页分开处理：
    # 这种页需要真人在浏览器窗口点一下，程序应停下来等，而不是烧重试。
    CHALLENGE_MARKERS = (
        "验证您是人类", "驗證您是人類", "正在执行安全验证", "正在執行安全驗證",
        "Verify you are human", "Just a moment", "challenges.cloudflare.com",
    )
    HUMAN_WAIT_SECONDS = 180   # 等用户手动过验证的最长秒数
    POLL_SECONDS = 2
    CLICK_WAIT_SECONDS = 9     # 撳页后等新数据的最长秒数
    CLICK_BAIL_SECONDS = 5     # 撳页后 URL 与数据都无变化的提前放弃秒数

    def __init__(self, start_url: str, profile_dir: str, headless: bool = False):
        self._base_url = start_url
        self.page_size_hint = None
        self._nav_count = 0
        self._current_page = None    # 浏览器当前停留的页码（供撳页前进判断）
        self._last_first_id = None   # 当前页第一个岗位 id（判断翻页是否生效）
        self._click_fail_streak = 0  # 撳页连续失败次数，两次后本次运行停用撳页
        super().__init__(start_url, profile_dir, headless=headless)

    def _nav_url(self, page: int) -> str:
        parsed = urllib.parse.urlparse(self._base_url)
        qs = dict(urllib.parse.parse_qsl(parsed.query))
        if page > 1:
            qs["page"] = str(page)
        else:
            qs.pop("page", None)
        return urllib.parse.urlunparse(parsed._replace(
            query=urllib.parse.urlencode(qs)))

    def _redux(self):
        """取当前页的 SEEK_REDUX_DATA：先 evaluate，失败再解析 HTML。"""
        data = None
        try:
            data = self._page.evaluate("() => window.SEEK_REDUX_DATA || null")
        except Exception:
            data = None
        if not isinstance(data, dict):
            try:
                data = _extract_redux_json(self._page.content())
            except Exception:
                data = None
        return data if isinstance(data, dict) else None

    def _probe_text(self) -> str:
        try:
            return self._page.evaluate(
                "() => (document.title + ' ' + "
                "(document.body ? document.body.innerText.slice(0, 600) : ''))"
            ) or ""
        except Exception:
            return ""

    def _is_challenge(self) -> bool:
        text = self._probe_text()
        return any(m in text for m in self.CHALLENGE_MARKERS)

    def _payload(self, data: dict):
        results = (data.get("results") or {}).get("results") or {}
        jobs = results.get("jobs") or []
        total = None
        for cand in (results.get("totalCount"), (data.get("results") or {}).get("totalCount"),
                     data.get("totalCount")):
            if isinstance(cand, int) and cand >= 0:
                total = cand
                break
        if jobs:
            self.page_size_hint = max(self.page_size_hint or 0, len(jobs))
        return 200, json.dumps({"data": jobs, "totalCount": total},
                               ensure_ascii=False)

    def fetch_job_jd(self, job_id: str):
        """打开岗位详情页提取 JD 全文。

        返回 JD 文本（可能为空字符串 = 页面正常但没找到 JD）；
        被 Cloudflare 硬封锁/验证无法通过时返回 None，调用方应停下并存档。
        中文页提取不到时按原 fetch_jd.py 的做法再试英文页。
        """
        self._current_page = None  # 离开搜索页，撳页前进的前提已失效
        for tmpl in ("https://hk.jobsdb.com/zh/job/{id}",
                     "https://hk.jobsdb.com/job/{id}"):
            result = self._fetch_jd_once(tmpl.format(id=job_id))
            if result:
                return result
            if result is None:
                return None
        return ""

    def _fetch_jd_once(self, url: str):
        time.sleep(random.uniform(1.0, 2.0))
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            return ""
        deadline = time.time() + 20
        solved_once = False
        while True:
            try:
                html = self._page.content()
            except Exception:
                html = ""
            jd = extract_jd_from_html(html)
            if jd:
                return jd
            if self._is_challenge():
                if solved_once:
                    return None
                solved_once = True
                if self._wait_for_solution(url) is None:
                    return None
                deadline = time.time() + 20
                continue
            if html and _looks_blocked(html):
                return None
            if time.time() >= deadline:
                return ""
            time.sleep(self.POLL_SECONDS)

    @staticmethod
    def _first_id(data: dict):
        try:
            jobs = (data.get("results") or {}).get("results", {}).get("jobs") or []
            return str(jobs[0].get("id")) if jobs else None
        except Exception:
            return None

    def _mark(self, page: int, data: dict):
        self._current_page = page
        fid = self._first_id(data)
        if fid:
            self._last_first_id = fid

    def _try_auto_click(self) -> bool:
        """尝试自动勾选 Turnstile：找到 challenges.cloudflare.com 的 iframe 点其勾选框。"""
        frame = None
        try:
            for f in self._page.frames:
                if "challenges.cloudflare.com" in (f.url or ""):
                    frame = f
                    break
        except Exception:
            return False
        if frame is None:
            return False
        print("  [验证] 先试自动勾选...", file=sys.stderr)
        clicked = False
        for selector, kwargs in (
            ("body", {"position": {"x": 26, "y": 32}}),
            ("input[type=checkbox]", {}),
        ):
            try:
                frame.click(selector, timeout=4000, **kwargs)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return False
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(1)
            if self._redux() is not None:
                print("  [OK] 自动勾选通过。", file=sys.stderr)
                return True
            if not self._is_challenge():
                time.sleep(1)
                if self._redux() is not None:
                    print("  [OK] 自动勾选通过。", file=sys.stderr)
                    return True
        return False

    def _wait_for_solution(self, url: str):
        """处理互动验证：先自动勾选，失败才等用户手动点。通过返回 redux 数据。"""
        if self._try_auto_click():
            data = self._redux()
            if data is not None:
                return data
        print("\n  [需要你操作] 自动勾选未通过，请在 Chrome 窗口里点一下"
              "「验证您是人类」那个勾选框。", file=sys.stderr)
        print(f"  [等待中] 程序会等 {self.HUMAN_WAIT_SECONDS} 秒，验证通过后自动继续，"
              "不用重跑。", file=sys.stderr)
        deadline = time.time() + self.HUMAN_WAIT_SECONDS
        reloaded = False
        while time.time() < deadline:
            time.sleep(self.POLL_SECONDS)
            data = self._redux()
            if data is not None:
                print("  [OK] 验证已通过，继续抓取。", file=sys.stderr)
                return data
            if not reloaded and not self._is_challenge():
                # 验证页已消失但数据还没出来，重新打开目标页一次
                reloaded = True
                try:
                    self._page.goto(url, wait_until="domcontentloaded",
                                    timeout=60000)
                except Exception:
                    pass
        return None

    def _click_next(self, target_page: int):
        """撳页面上的分页链接前进一页（走网站自己的流程，比直跳 URL 少触发验证）。

        成功返回新页 redux 数据；失败返回 None（调用方改用直跳 URL）。
        """
        clicked = False
        selectors = (
            f'a[href*="page={target_page}"]',
            'a[rel="next"]',
            'a[aria-label="Next"]',
            'a[aria-label="下一頁"]',
            'a[aria-label="下一页"]',
        )
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    loc.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            for label in ("下一頁", "下一页", "Next"):
                try:
                    self._page.get_by_text(label, exact=False).first.click(timeout=3000)
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            return None
        print(f"  [导航] 已撳分页链接去第 {target_page} 页", file=sys.stderr)
        # 实测（2026-11-20）：该站分页是页面内换数据，靠的正是被拦的 API，
        # 在自动化浏览器里撳了也加载不出新数据（页码/内容都不变），只有整页
        # 直跳（服务器渲染）才有数据。所以这里只等 9 秒，且 5 秒内 URL 与
        # 数据都毫无变化就提前放弃，交给直跳处理，不再每页白等 25 秒。
        try:
            start_url = self._page.url
        except Exception:
            start_url = None
        started = time.time()
        deadline = started + self.CLICK_WAIT_SECONDS
        while time.time() < deadline:
            data = self._redux()
            if data is not None and self._first_id(data) not in (None, self._last_first_id):
                return data
            if self._is_challenge():
                return self._wait_for_solution(self._nav_url(target_page))
            if time.time() - started > self.CLICK_BAIL_SECONDS and start_url is not None:
                try:
                    if self._page.url == start_url:
                        return None
                except Exception:
                    pass
            time.sleep(self.POLL_SECONDS)
        return None

    def get(self, params: dict, timeout: int = 30):
        page = int(params.get("page", 1))
        url = self._nav_url(page)
        # 放慢导航节奏，降低触发验证的频率（首个页面除外）
        if self._nav_count > 0:
            time.sleep(random.uniform(2, 4))
        self._nav_count += 1

        # 顺序翻页优先撳分页链接（真人流程）；失败才直跳 URL。
        # 连续失败两次说明本站点分页在当前浏览器里走不通，本次运行停用撳页。
        if page > 1 and self._current_page == page - 1 \
                and self._click_fail_streak < 2:
            data = self._click_next(page)
            if data is not None:
                self._click_fail_streak = 0
                self._mark(page, data)
                return self._payload(data)
            self._click_fail_streak += 1
            if self._click_fail_streak >= 2:
                print("  [导航] 撳页多次无效，本次运行改用直跳 URL 翻页",
                      file=sys.stderr)
            else:
                print("  [导航] 撳页前进失败，改用直跳 URL", file=sys.stderr)

        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return 503, f"页面打开失败: {e}"

        # 轮询等数据出现（渲染需要时间）；途中出现验证页就处理验证
        deadline = time.time() + 20
        while True:
            data = self._redux()
            if data is not None:
                self._mark(page, data)
                return self._payload(data)
            if self._is_challenge():
                data = self._wait_for_solution(url)
                if data is not None:
                    self._mark(page, data)
                    return self._payload(data)
                return 403, "互动验证未在等待时间内完成"
            if time.time() >= deadline:
                break
            time.sleep(self.POLL_SECONDS)

        try:
            body = self._page.content()
        except Exception:
            body = ""
        if _looks_blocked(body):
            return 403, body[:500]
        return 503, "SEEK_REDUX_DATA not found"


# 兼容旧名
_Client = _ApiClient


def _looks_blocked(text: str) -> bool:
    return any(m in text for m in BLOCK_MARKERS)


def fetch_page(client: _Client, params: dict, page: int) -> dict:
    """抓取一页 API 结果。被封锁/限流/5xx 时按 RETRY_WAITS 退避重试。"""
    q = dict(params)
    q["page"] = page

    last_err = ""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            status, text = client.get(q, timeout=30)
        except Exception as e:
            status, text = None, ""
            last_err = f"请求异常: {e}"

        if status == 200 and text and not _looks_blocked(text):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                return payload
            last_err = "返回内容不是预期的 API JSON"
        elif status in (403, 429) or (text and _looks_blocked(text)):
            last_err = f"被 Cloudflare 拦截 (HTTP {status})"
        elif status is not None and 500 <= status < 600:
            last_err = f"服务器错误 (HTTP {status})"
        elif status is not None:
            raise FetchError(f"第 {page} 页请求失败: HTTP {status}")
        # status is None（网络异常）也走重试

        if attempt < len(RETRY_WAITS):
            wait = RETRY_WAITS[attempt]
            print(f"  [WARN] 第 {page} 页{last_err}，{wait}s 后重试 "
                  f"({attempt + 1}/{len(RETRY_WAITS)})", file=sys.stderr)
            time.sleep(wait)

    raise BlockedError(f"第 {page} 页重试 {len(RETRY_WAITS)} 次后仍失败: {last_err}")


# ── URL 解析 ─────────────────────────────────────────────────────────────

def parse_search_url(url: str):
    """从搜索 URL 提取 API 参数，返回 (params, keyword_slug)。

    支持:
      /zh/{keyword}-jobs                 → keywords
      /zh/{classification}-jobs          → classification（slug 在映射表时）
      /zh/{keyword}-jobs/in-{location}   → where
      /zh/jobs-in-{location}/...         → where
      query 参数按 _QUERY_PASSTHROUGH 透传（keywords/where/classification 等）
    """
    params = {"siteKey": "HK-Main", "sourcesystem": "houston"}
    keyword_slug = None
    keyword_text = None  # 保留原始大小写的关键词文本（如 ESG）

    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/")
    segments = [s for s in path.split("/") if s]

    for seg in segments:
        low = seg.lower()
        if low in ("zh", "en", "jobs"):
            continue
        if low.startswith("jobs-in-"):
            params["where"] = seg[len("jobs-in-"):].replace("-", " ")
            continue
        if low.startswith("in-") and "where" not in params:
            params["where"] = seg[len("in-"):].replace("-", " ")
            continue
        if low.startswith("jobs-"):
            rest = low[len("jobs-"):]
            if rest in CLASSIFICATION_MAP:
                params["classification"] = CLASSIFICATION_MAP[rest]
            else:
                keyword_slug = rest
                keyword_text = seg[len("jobs-"):].replace("-", " ")
            continue
        if low.endswith("-jobs"):
            slug = low[: -len("-jobs")]
            if slug in CLASSIFICATION_MAP:
                params["classification"] = CLASSIFICATION_MAP[slug]
            else:
                keyword_slug = slug
                keyword_text = seg[: -len("-jobs")].replace("-", " ")
            continue
        if low in CLASSIFICATION_MAP:
            params["classification"] = CLASSIFICATION_MAP[low]
        elif low in WORKTYPE_MAP:
            params["worktype"] = WORKTYPE_MAP[low]
        elif low in WORK_ARRANGEMENT_MAP:
            params["workarrangement"] = WORK_ARRANGEMENT_MAP[low]

    qs = urllib.parse.parse_qs(parsed.query)
    for key in _QUERY_PASSTHROUGH:
        if key in qs and qs[key][0] != "":
            params[key] = qs[key][0]

    if keyword_text and "keywords" not in params:
        params["keywords"] = keyword_text

    return params, keyword_slug


def extract_keyword_from_url(url: str):
    """兼容旧接口：从 URL 提取关键词 slug。"""
    _, slug = parse_search_url(url)
    if slug:
        return slug
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "keywords" in qs:
        return qs["keywords"][0]
    return None


# ── 抓取主循环 ───────────────────────────────────────────────────────────

def fetch_all(params: dict, max_pages: int = 0, start_page: int = 1,
              known_ids: set = None, on_checkpoint=None, client=None,
              min_delay: float = 0.8, max_delay: float = 1.8):
    """遍历所有分页。返回 (raw_jobs, total_count, complete, next_page)。

    raw_jobs 只含本次新抓的岗位；known_ids 是此前已抓岗位的 ID 集合，
    用于续爬去重。on_checkpoint(raw_jobs, total_count, complete, next_page)
    在每页后调用，用于逐页落盘；被封锁时抛 BlockedError，由调用方退出，
    已完成页的进度已通过 on_checkpoint 保存。
    """
    if client is None:
        client = _ApiClient()
    page_size = int(params.get("pageSize", DEFAULT_PAGE_SIZE))

    all_jobs = []
    seen = set(known_ids or ())
    total = None
    page = start_page
    run_new = 0        # 本次运行累计新增数
    dup_streak = 0     # 「已进入新数据区后」连续整页重复的次数

    while True:
        if max_pages > 0 and page > max_pages:
            complete = total is not None and len(seen) >= total
            return all_jobs, total, complete, (None if complete else page)

        payload = fetch_page(client, params, page)
        data = payload.get("data") or []
        if isinstance(payload.get("totalCount"), int):
            total = payload["totalCount"]

        new = 0
        for job in data:
            jid = str(job.get("id", ""))
            if jid and jid not in seen:
                seen.add(jid)
                all_jobs.append(job)
                new += 1
        run_new += new

        # 导航收割模式下每页数量以网站实际返回为准（客户端会给出提示）
        eff_page_size = getattr(client, "page_size_hint", None) or page_size
        fetched_upto = (page - 1) * eff_page_size + len(data)
        print(f"  [API] 第 {page} 页返回 {len(data)} 个，新增 {new} 个，"
              f"累计 {len(all_jobs)} 个"
              + (f" / 共 {total} 个" if total is not None else ""),
              file=sys.stderr)

        # 整页重复的两种含义要分清：
        # ① 续爬旧存档时，开头几页本来就是已存岗位（新增 0 是正常的，
        #    绝不能因此判定抓完——v3 的 bug 就是误判于此）；
        # ② 已经抓到新数据之后，网站把尾页重复返回（翻过头了）。
        # 只有 ② 才算收尾信号，且要求连续两页重复才停，防偶发误判。
        if new == 0 and data and run_new > 0:
            dup_streak += 1
        elif new > 0:
            dup_streak = 0
        if new == 0 and data and run_new == 0 and total is not None \
                and fetched_upto < total:
            print("  [续爬] 本页全是已存岗位，继续下一页找新数据...",
                  file=sys.stderr)

        # 收尾条件：空页 / 不满一页 / 已达到 totalCount / 进入新数据区后连续两页整页重复
        complete = (
            not data
            or len(data) < eff_page_size
            or (total is not None and fetched_upto >= total)
            or dup_streak >= 2
        )
        next_page = None if complete else page + 1
        if on_checkpoint:
            on_checkpoint(all_jobs, total, complete, next_page)
        if complete:
            return all_jobs, total, True, None

        page += 1
        time.sleep(random.uniform(min_delay, max_delay))


# ── JD 阶段（合并自 fetch_jd.py，用导航浏览器逐个开详情页）────────────────

def load_jobs_file(path: str) -> list:
    """加载岗位 JSON（支持纯数组 / {"jobs": [...]} / {"data": [...]}）。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("jobs", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def load_baseline_ids(path: str) -> set:
    """从基准文件读岗位 ID 集合：.json（岗位数组）或 .csv（含 id 列）。"""
    ids = set()
    if path.lower().endswith(".csv"):
        import csv as _csv
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = _csv.DictReader(f)
            id_col = None
            for name in (reader.fieldnames or []):
                if name and name.strip().lower() in (
                        "id", "job_id", "jobid", "岗位id", "岗位编号", "職位id"):
                    id_col = name
                    break
            if id_col is None:
                print(f"[WARN] 基准 CSV 找不到 ID 列（现有列: {reader.fieldnames}），"
                      "忽略基准跳过", file=sys.stderr)
                return ids
            for row in reader:
                v = str(row.get(id_col, "")).strip()
                if v:
                    ids.add(v)
    else:
        for job in load_jobs_file(path):
            jid = str(job.get("id", "")).strip()
            if jid:
                ids.add(jid)
    return ids


def run_jd_phase(jobs: list, nav_client, save, force: bool = False,
                 baseline_ids: set = None, min_delay: float = 1.5,
                 max_delay: float = 3.0):
    """逐个岗位抓 JD，写回 job["jd"] 并在每个岗位后调用 save() 落盘。

    返回 (stats, blocked)：blocked=True 表示中途被封锁，已完成部分已落盘，
    其余岗位保持未抓状态，重跑即可续。已有 JD（>50 字）的岗位默认跳过，
    force=True 时重抓；baseline_ids 中的岗位视为已处理，跳过不抓。
    """
    stats = {"done": 0, "empty": 0, "skipped": 0, "baseline": 0}
    todo_idx = []
    for i, job in enumerate(jobs):
        jid = str(job.get("id", "")).strip()
        if baseline_ids and jid in baseline_ids:
            stats["baseline"] += 1
            continue
        if not force and len(job.get("jd") or "") > 50:
            stats["skipped"] += 1
            continue
        todo_idx.append(i)
    total_todo = len(todo_idx)
    if total_todo:
        print(f"[INFO] JD 阶段：共 {len(jobs)} 个岗位，需抓 {total_todo} 个"
              f"（已有 JD 跳过 {stats['skipped']} 个"
              + (f"，基准跳过 {stats['baseline']} 个" if stats["baseline"] else "")
              + "）", file=sys.stderr)
    for n, i in enumerate(todo_idx):
        job = jobs[i]
        jid = str(job.get("id", ""))
        print(f"  [JD {n + 1}/{total_todo}] 岗位 {jid}: "
              f"{str(job.get('title', ''))[:50]}", file=sys.stderr)
        jd = nav_client.fetch_job_jd(jid)
        if jd is None:
            save()
            print("[WARN] JD 阶段被 Cloudflare 拦住，已保存已完成部分；"
                  "稍后重跑同一命令即可续抓未完成的岗位。", file=sys.stderr)
            return stats, True
        job["jd"] = jd
        if jd:
            stats["done"] += 1
            print(f"    ✓ JD {len(jd)} 字符", file=sys.stderr)
        else:
            stats["empty"] += 1
            print("    ✗ 未找到 JD（页面无 JD 或岗位已下架）", file=sys.stderr)
        save()
        if n < total_todo - 1:
            time.sleep(random.uniform(min_delay, max_delay))
    return stats, False


# ── 结果扁平化（字段与旧版保持一致，下游脚本无需改动）────────────────────

def _desc(node) -> str:
    """从 API 的 {description} / {label:{text}} / 字符串节点取文本。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("description"):
            return node["description"]
        label = node.get("label")
        if isinstance(label, dict):
            return label.get("text", "") or ""
        if isinstance(label, str):
            return label
    return ""


def flatten_job(job: dict, params: dict = None) -> dict:
    classifications = job.get("classifications", []) or []
    subclass_name = ""
    class_name = ""
    if classifications and isinstance(classifications[0], dict):
        subclass_name = _desc(classifications[0].get("subclassification"))
        class_name = _desc(classifications[0].get("classification"))

    locations = job.get("locations", []) or []
    parts = []
    for loc in locations[:2]:
        label = _desc(loc)
        if label:
            parts.append(label)
    location_str = ", ".join(parts)

    salary = job.get("salaryLabel") or "面议"

    advertiser = job.get("advertiser", {}) or {}
    # 修正旧版 bug：旧版 title 误取 advertiser.description（那是公司名）。
    title = job.get("title", "") or ""
    company = job.get("companyName", "") or advertiser.get("description", "")

    job_id = str(job.get("id", ""))
    link = f"https://hk.jobsdb.com/zh/job/{job_id}"

    work_types = []
    for w in job.get("workTypes", []) or []:
        t = _desc(w)
        if t:
            work_types.append(t)

    return {
        "id": job_id,
        "company": company,
        "title": title,
        "location": location_str,
        "salary": salary,
        "industry": class_name,
        "subclass": subclass_name,
        "link": link,
        "listing_date": job.get("listingDate", ""),
        "teaser": job.get("teaser", ""),
        "bullet_points": job.get("bulletPoints", []) or [],
        "work_types": work_types,
        "is_featured": job.get("isFeatured", False),
    }


# ── 输出 / 断点 ──────────────────────────────────────────────────────────

def _atomic_write_json(path: str, obj):
    """原子写 JSON（先写临时文件再替换），中途中断也不会写坏已有文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_checkpoint(path: str):
    """读取已有输出文件作为续爬种子，返回 (扁平岗位列表, next_page)。

    已抓岗位直接沿用落盘的扁平结果，续爬只按 id 去重，不再重复请求。
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    next_page = data.get("next_page") if isinstance(data, dict) else None
    return jobs, next_page


def main():
    parser = argparse.ArgumentParser(description="从 Jobsdb HK 抓取岗位列表（API 版）")
    parser.add_argument("url", nargs="?", help="搜索页面 URL")
    parser.add_argument("-o", "--output", default="all_jobs.json", help="输出 JSON 文件路径")
    parser.add_argument("--max-pages", type=int, default=0, help="最大页数限制 (0=全部)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="每页数量 (API 上限 100)")
    parser.add_argument("--keyword", help="手动指定搜索关键词（可含空格）")
    parser.add_argument("--where", help="地点筛选，如 Hong Kong")
    parser.add_argument("--classification-id", help="分类 ID（对应 API 的 classification 参数）")
    parser.add_argument("--start-page", type=int, default=1, help="起始页（默认 1）")
    parser.add_argument("--resume", action="store_true",
                        help="从输出文件上次中断处续爬（保留已抓岗位）")
    parser.add_argument("--first-page-only", action="store_true", help="仅获取第 1 页（测试用）")
    parser.add_argument("--mode", choices=["auto", "api", "browser", "nav"], default="auto",
                        help="传输模式：auto=依次试直连→浏览器fetch→导航收割（默认），api=仅直连，browser=仅浏览器fetch，nav=仅导航收割")
    parser.add_argument("--headless", action="store_true",
                        help="browser 模式无头运行（不建议：有头模式才能手动过验证）")
    parser.add_argument("--profile-dir", default=".jobsdb-profile",
                        help="browser 模式的持久化 profile 目录（默认 .jobsdb-profile）")
    parser.add_argument("--with-jd", action="store_true",
                        help="列表抓完后接着抓 JD 全文（同一浏览器，一条命令完成，替代 fetch_jd.py）")
    parser.add_argument("--jd-input", help="JD 独立模式：只对已有岗位 JSON 抓 JD，不抓列表（替代 fetch_jd.py 的 --input）")
    parser.add_argument("--jd-output", help="JD 独立模式的输出文件（默认覆盖 --jd-input 文件）")
    parser.add_argument("--jd-baseline", help="基准文件（CSV/JSON）：其中 ID 的岗位跳过 JD 不抓")
    parser.add_argument("--force-jd", action="store_true",
                        help="已有 JD 的岗位也重新抓（默认跳过）")

    args = parser.parse_args()

    # ── JD 独立模式：只抓 JD，不抓列表 ──
    if args.jd_input:
        jobs = load_jobs_file(args.jd_input)
        jd_out = args.jd_output or args.jd_input
        print(f"[INFO] JD 模式：{args.jd_input} 共 {len(jobs)} 个岗位 → {jd_out}",
              file=sys.stderr)
        if not jobs:
            print("[INFO] 输入文件没有岗位，不启动浏览器，直接结束。"
                  "（通常是去重后没有新岗位，属正常情况）", file=sys.stderr)
            _atomic_write_json(jd_out, jobs)
            return
        profile_dir = os.path.abspath(args.profile_dir)
        start = jobs[0].get("link") if jobs else None
        try:
            nav = _BrowserNavClient(start or "https://hk.jobsdb.com/zh/jobs",
                                    profile_dir, headless=args.headless)
        except FetchError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)

        def _save_jd():
            _atomic_write_json(jd_out, jobs)

        baseline_ids = load_baseline_ids(args.jd_baseline) if args.jd_baseline else None
        try:
            stats, blocked = run_jd_phase(
                jobs, nav, _save_jd, force=args.force_jd,
                baseline_ids=baseline_ids)
        finally:
            nav.close()
        print(f"\n[DONE] JD 完成：新抓 {stats['done']} 个，无 JD {stats['empty']} 个，"
              f"跳过 {stats['skipped'] + stats['baseline']} 个 → {jd_out}"
              + ("（中途被拦，未抓部分重跑同一命令续抓）" if blocked else ""),
              file=sys.stderr)
        return

    params = {"siteKey": "HK-Main", "sourcesystem": "houston"}
    keyword = None

    if args.url:
        url = re.sub(r"[?&]token=[^&]*", "", args.url)
        url = re.sub(r"[?&]ref=[^&]*", "", url)
        params, keyword = parse_search_url(url)
        print(f"[INFO] 从 URL 解析参数: {json.dumps(params, ensure_ascii=False)}",
              file=sys.stderr)

    if args.keyword:
        keyword = args.keyword
        params["keywords"] = args.keyword
        print(f"[INFO] 使用手动指定的关键词: {args.keyword}", file=sys.stderr)
    if args.where:
        params["where"] = args.where
    if args.classification_id:
        params["classification"] = args.classification_id

    if "keywords" not in params and "classification" not in params:
        print("[ERROR] 需要搜索关键词或分类：请提供搜索 URL，或用 --keyword / --classification-id",
              file=sys.stderr)
        sys.exit(1)

    params["pageSize"] = max(1, min(int(args.page_size), 100))

    # ── 断点续爬准备 ──
    seed_flat = []       # 已落盘的扁平 job（续爬时沿用）
    start_page = args.start_page
    if args.resume and os.path.exists(args.output):
        try:
            seed_flat, saved_next = load_checkpoint(args.output)
            if saved_next:
                start_page = saved_next
            print(f"[INFO] 续爬：已有 {len(seed_flat)} 个岗位，从第 {start_page} 页继续",
                  file=sys.stderr)
        except Exception as e:
            print(f"[WARN] 读取已有输出失败，改为从头抓取: {e}", file=sys.stderr)
            seed_flat = []

    # max_pages 为绝对页数上限（第 1 页起计），续爬时同样适用
    max_pages = 1 if args.first_page_only else args.max_pages

    def checkpoint(raw_jobs, total, complete, next_page):
        merged_raw = raw_jobs
        flat = [flatten_job(j, params) for j in merged_raw]
        payload = {
            "total": len(seed_flat) + len(flat),
            "total_count": total,
            "complete": complete,
            "next_page": next_page,
            "search_params": params,
            "keyword": keyword,
            "jobs": seed_flat + flat,
        }
        tmp = args.output + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.output)

    # ── 选择传输模式 ──
    client = None
    browser_client = None
    start_url = args.url
    if not start_url and keyword:
        slug = str(keyword).strip().lower().replace(" ", "-")
        start_url = f"https://hk.jobsdb.com/zh/{slug}-jobs"
    if not start_url:
        start_url = "https://hk.jobsdb.com/zh/jobs"
    profile_dir = os.path.abspath(args.profile_dir)

    def _probe_ok(status, text):
        if status != 200 or not text or _looks_blocked(text):
            return False
        try:
            return isinstance(json.loads(text).get("data"), list)
        except Exception:
            return False

    def _launch(cls):
        try:
            return cls(start_url, profile_dir, headless=args.headless)
        except FetchError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)

    def _use_nav():
        if args.classification_id or args.where:
            print("[WARN] 导航收割模式按搜索 URL/关键词抓取，"
                  "--classification-id / --where 不生效（请把条件写进搜索 URL）",
                  file=sys.stderr)
        bc = _launch(_BrowserNavClient)
        print(f"[INFO] 传输模式：浏览器导航收割（profile: {profile_dir}）",
              file=sys.stderr)
        return bc

    if args.mode == "api":
        client = _ApiClient()
    elif args.mode == "browser":
        browser_client = _launch(_BrowserClient)
        client = browser_client
        print(f"[INFO] 传输模式：浏览器fetch（profile: {profile_dir}）", file=sys.stderr)
    elif args.mode == "nav":
        browser_client = _use_nav()
        client = browser_client
    else:  # auto：直连 → 浏览器fetch → 导航收割，逐级探测
        api_client = _ApiClient()
        probe = dict(params)
        probe["page"] = start_page
        try:
            p_status, p_text = api_client.get(probe, timeout=30)
            api_ok = _probe_ok(p_status, p_text)
        except Exception:
            api_ok = False
        if api_ok:
            client = api_client
            print("[INFO] 传输模式：API 直连（探测通过）", file=sys.stderr)
        else:
            print("[INFO] API 直连被拦截，试浏览器 fetch 模式...", file=sys.stderr)
            bc = _launch(_BrowserClient)
            try:
                b_status, b_text = bc.get(probe, timeout=30)
                fetch_ok = _probe_ok(b_status, b_text)
            except Exception:
                fetch_ok = False
            if fetch_ok:
                browser_client = bc
                client = bc
                print("[INFO] 传输模式：浏览器fetch（探测通过）", file=sys.stderr)
            else:
                print("[INFO] 浏览器 fetch 也被拦截，切换到导航收割模式...",
                      file=sys.stderr)
                bc.close()
                browser_client = _use_nav()
                client = browser_client

    try:
        raw_jobs, total, complete, next_page = fetch_all(
            params,
            max_pages=max_pages,
            start_page=start_page,
            known_ids={str(j.get("id", "")) for j in seed_flat if j.get("id")},
            on_checkpoint=checkpoint,
            client=client,
        )
    except (BlockedError, FetchError) as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        if seed_flat or (os.path.exists(args.output)):
            print(f"[INFO] 已抓到的岗位已保存在 {args.output}，"
                  f"稍后用同一命令加 --resume 即可从中断页续爬，不必从头来。",
                  file=sys.stderr)
        else:
            print("[INFO] 第 1 页就被拦截：若用浏览器模式，请在弹出的 Chrome "
                  "窗口里手动完成 Cloudflare 验证后重新运行；若仍不行，"
                  "多为当前 IP 被封锁，请换家庭网络/关闭数据中心 VPN，"
                  "等待一段时间后再试。",
                  file=sys.stderr)
        if browser_client is not None:
            browser_client.close()
        sys.exit(1)

    total_jobs = len(seed_flat) + len(raw_jobs)
    if total_jobs == 0:
        if browser_client is not None:
            browser_client.close()
        print("[ERROR] 未获取到任何岗位", file=sys.stderr)
        sys.exit(1)

    state = "全部抓完" if complete else f"已达页数上限，下次从第 {next_page} 页续爬（--resume）"
    print(f"\n[DONE] 共 {total_jobs} 个岗位 → {args.output}（{state}）", file=sys.stderr)

    # ── JD 阶段（--with-jd）：沿用/另开导航浏览器，逐个岗位抓 JD ──
    if args.with_jd:
        flat_jobs = seed_flat + [flatten_job(j, params) for j in raw_jobs]

        def _save_payload():
            _atomic_write_json(args.output, {
                "total": len(flat_jobs),
                "total_count": total,
                "complete": complete,
                "next_page": next_page,
                "search_params": params,
                "keyword": keyword,
                "jobs": flat_jobs,
            })

        nav = browser_client if isinstance(browser_client, _BrowserNavClient) else None
        own_nav = None
        if nav is None:
            own_nav = _launch(_BrowserNavClient)
            nav = own_nav
        baseline_ids = load_baseline_ids(args.jd_baseline) if args.jd_baseline else None
        try:
            stats, blocked = run_jd_phase(
                flat_jobs, nav, _save_payload, force=args.force_jd,
                baseline_ids=baseline_ids)
        finally:
            if own_nav is not None:
                own_nav.close()
            if browser_client is not None:
                browser_client.close()
        print(f"[DONE] JD 阶段：新抓 {stats['done']} 个，无 JD {stats['empty']} 个，"
              f"跳过 {stats['skipped'] + stats['baseline']} 个"
              + ("（中途被拦，重跑同一命令可续）" if blocked else ""),
              file=sys.stderr)
    elif browser_client is not None:
        browser_client.close()


if __name__ == "__main__":
    main()
