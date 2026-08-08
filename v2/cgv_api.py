"""CGV 신규 BFF API 클라이언트.

리뉴얼 이후 `ticket.cgv.co.kr/CGV2011/...` 레거시 엔드포인트는 폐기(404)되었다.
현재 웹은 Next.js 기반이며, 브라우저는 `api.cgv.co.kr`(무인증 호출 시 401/403)이 아니라
`https://cgv.co.kr/api/v1/...` BFF 프록시를 경유한다. BFF는 별도 인증/서명 없이
GET 호출이 가능하므로 헤드리스 브라우저 없이 순수 HTTP로 폴링할 수 있다.

경로 매핑 (CGV 프론트엔드 번들의 매핑 테이블 기준):
    /cnm/atkt/*  ->  https://cgv.co.kr/api/v1/booking/*
    /cnm/*       ->  https://cgv.co.kr/api/v1/content/*
"""

import json
import random
import time
import urllib.parse

import requests

BOOKING_BASE = "https://cgv.co.kr/api/v1/booking"
CONTENT_BASE = "https://cgv.co.kr/api/v1/content"

# CGV 회사코드
CO_CD = "A420"

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR",
    "Referer": "https://cgv.co.kr/",
    "Origin": "https://cgv.co.kr",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


class CgvApiError(RuntimeError):
    pass


class CgvBlockedError(CgvApiError):
    """CGV가 요청을 거부하거나 접근을 제한한 상태.

    이 예외가 발생하면 즉시 요청을 대폭 줄여야 한다. 우회하지 않는다.
    """


# CGV 차단 안내 페이지에 나타나는 문구.
BLOCK_MARKERS = ("비정상적으로", "이용이 제한", "RAY_ID")


class CgvApi:
    def __init__(self, timeout=8.0, max_retries=3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._site_name_cache = {}

    def _get(self, base, path, params):
        params = dict(params)
        params.setdefault("coCd", CO_CD)
        url = f"{base}/{path}"

        last_error = None
        for attempt in range(self.max_retries):
            if attempt:
                # 지수 백오프 + 지터. 동시 실행되는 여러 워커가 같은 타이밍에
                # 재시도하며 서버를 두드리는 것을 방지한다.
                time.sleep(min(2 ** attempt * 0.5, 5.0) + random.uniform(0, 0.3))
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                continue

            if response.status_code == 429:
                # 서버가 명시적으로 속도를 줄이라고 알린 상태.
                raise CgvBlockedError(f"{path} HTTP 429 (요청이 너무 잦음)")
            if response.status_code in (401, 403):
                raise CgvBlockedError(f"{path} HTTP {response.status_code} (접근 거부)")
            if response.status_code >= 500:
                last_error = CgvApiError(f"{path} HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise CgvApiError(f"{path} HTTP {response.status_code}")

            text = response.content.decode("utf-8-sig", errors="replace")
            if any(marker in text for marker in BLOCK_MARKERS):
                raise CgvBlockedError(f"{path} 접근이 제한되었습니다")

            try:
                payload = json.loads(text)
            except ValueError as exc:
                last_error = CgvApiError(f"{path} JSON 파싱 실패: {exc}")
                continue

            status = str(payload.get("statusCode"))
            if status not in ("0", "200"):
                raise CgvApiError(f"{path} statusCode={status} {payload.get('statusMessage')}")
            return payload.get("data")

        raise CgvApiError(f"{path} 요청 실패: {last_error}")

    def open_dates(self, site_no):
        """해당 극장에서 현재 예매 가능한 상영일(YYYYMMDD) 목록.

        응답이 700바이트 수준으로 매우 가벼워 고빈도 폴링에 적합하다.
        예매 오픈으로 상영일이 추가되면 이 목록이 먼저 늘어난다.
        """
        data = self._get(BOOKING_BASE, "searchSiteScnscYmdListBySite", {"siteNo": site_no}) or []
        return [row["scnYmd"] for row in data if row.get("scnYmd")]

    def last_scn_day(self, site_no):
        """예매 가능한 마지막 상영일. 오픈 지평선이 늘어나는지 감시하는 용도."""
        data = self._get(BOOKING_BASE, "searchLastScnDay", {"siteNo": site_no}) or []
        return data[0].get("scnYmd") if data else None

    def schedules(self, site_no, scn_ymd, scns_no=""):
        """특정 극장/일자의 상영 회차 목록.

        scns_no(상영관 번호)를 지정하면 응답이 220KB에서 10KB 수준으로 줄어든다.
        """
        return self._get(
            BOOKING_BASE,
            "searchMovScnInfo",
            {
                "siteNo": site_no,
                "scnYmd": scn_ymd,
                "scnsNo": scns_no,
                "scnSseq": "",
                "rtctlScopCd": "08",
                "custNo": "",
            },
        ) or []

    def sites(self):
        """전체 지역/극장 목록. 극장 코드를 조회할 때 사용한다."""
        data = self._get(CONTENT_BASE, "site/searchAllRegionAndSite", {}) or {}
        return data.get("siteInfo") or []

    def site_name(self, site_no):
        """극장 코드에 해당하는 극장명. 예매 딥링크를 만들 때 필요하다."""
        if site_no not in self._site_name_cache:
            for site in self.sites():
                self._site_name_cache[str(site.get("siteNo"))] = site.get("siteNm") or ""
        return self._site_name_cache.get(site_no, "")


def booking_url(site_no, scn_ymd, site_nm=""):
    """해당 극장·날짜의 상영시간표 페이지 주소.

    예매 페이지는 URL의 siteNo/scnYmd를 그대로 사용해 해당 극장·날짜의 시간표를
    조회한다. 다만 `siteNm`이 없으면 시간표가 렌더링되지 않으므로 반드시 함께 넘긴다.
    (실측: siteNm 없이 접속하면 시간표 영역이 비어 있음)
    """
    params = {"siteNo": site_no, "siteNm": site_nm, "scnYmd": scn_ymd}
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return f"https://cgv.co.kr/cnm/movieBook/cinema?{query}"
