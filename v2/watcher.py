"""예매 오픈 감시 코어.

감시 전략(2단 구조):

  1단 - 예매 가능 일자 감시 (기본 2초)
        `searchSiteScnscYmdListBySite`는 응답이 ~700B로 매우 가볍다.
        예매 오픈으로 상영일이 추가되면 이 목록이 가장 먼저 늘어나므로,
        새 일자가 감지되면 즉시 해당 일자의 회차를 조회한다.

  2단 - 회차 스윕 (기본 15초 / 전체 120초)
        이미 열려 있던 날짜에 회차만 추가되는 경우를 잡는다.
        새 개봉작은 예매 창의 뒤쪽 날짜에 먼저 붙는 경향이 있어,
        뒤쪽 구간을 자주 돌고 전체 구간은 더 긴 주기로 돈다.

v1은 XML 응답 전체를 문자열 diff했기 때문에 무관한 필드(잔여좌석 등) 변경에도
오탐이 발생했다. v2는 회차 고유키만 비교하므로 오탐이 없다.
"""

import json
import logging
import os
import random
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from cgv_api import CgvApi, CgvApiError, CgvBlockedError, booking_url

log = logging.getLogger(__name__)

# 예매 창 뒤쪽 몇 일을 우선 감시할지. 신작 회차는 보통 뒤쪽에 먼저 붙는다.
PRIORITY_TAIL_DAYS = 14

# 전체 구간 스윕 주기(초).
FULL_SWEEP_INTERVAL = 300.0

# 주기마다 더할 무작위 지터 비율. 정확히 일정한 간격은 자동화 트래픽으로 식별되기 쉽다.
JITTER_RATIO = 0.25

# 차단이 감지되었을 때의 대기 시간(초). 단계적으로 늘어난다.
BLOCK_COOLDOWNS = (300.0, 900.0, 1800.0, 3600.0)

# 최초 실행이 아닌데도 새 회차가 이 수를 넘으면 CGV측 대규모 변경으로 보고
# 개별 알림 대신 요약만 보낸다.
BULK_ALERT_THRESHOLD = 60


def jitter(interval):
    """일정한 간격은 자동화로 식별되기 쉬우므로 약간의 무작위성을 준다."""
    return interval * (1.0 + random.uniform(0.0, JITTER_RATIO))


def showtime_key(row):
    """회차 고유키. 잔여좌석처럼 계속 바뀌는 값은 포함하지 않는다."""
    return "|".join(
        (
            str(row.get("scnYmd", "")),
            str(row.get("scnsNo", "")),
            str(row.get("scnSseq", "")),
            str(row.get("prodNo", "")),
        )
    )


def format_time(hhmm):
    hhmm = str(hhmm or "")
    return f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm


class StateStore:
    """감시 상태를 디스크에 보관한다. 재시작 후 중복 알림을 막기 위함이다."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def get(self, target_name):
        with self._lock:
            return self._data.get(target_name)

    def put(self, target_name, dates, keys):
        with self._lock:
            self._data[target_name] = {
                "dates": sorted(dates),
                "keys": sorted(keys),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._flush()

    def _flush(self):
        # 저장 중 프로세스가 죽어도 상태 파일이 깨지지 않도록 원자적으로 교체한다.
        directory = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


class TargetWatcher(threading.Thread):
    def __init__(self, target, notifier, state, settings):
        super().__init__(name=f"watcher-{target['name']}", daemon=True)
        self.target = target
        self.name_ = target["name"]
        self.site_no = target["site_no"]
        self.scns_no = target.get("scns_no", "")
        self.grade_cd = target.get("grade_cd", "")
        self.notifier = notifier
        self.state = state
        self.settings = settings
        self.api = CgvApi(timeout=settings["http_timeout"])
        self.site_nm = target.get("site_nm", "")
        self.known_dates = set()
        self.known_keys = set()
        self.block_count = 0
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------ 조회

    def _matches(self, row):
        if self.scns_no and str(row.get("scnsNo")) != self.scns_no:
            return False
        if self.grade_cd and str(row.get("tcscnsGradCd")) != self.grade_cd:
            return False
        return True

    def _fetch_date(self, scn_ymd):
        rows = self.api.schedules(self.site_no, scn_ymd, self.scns_no)
        return [row for row in rows if self._matches(row)]

    def _fetch_dates(self, dates):
        if not dates:
            return {}
        workers = max(1, min(self.settings["sweep_concurrency"], len(dates)))
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._fetch_date, d): d for d in dates}
            for future, scn_ymd in futures.items():
                try:
                    results[scn_ymd] = future.result()
                except CgvApiError as exc:
                    log.warning("[%s] %s 회차 조회 실패: %s", self.name_, scn_ymd, exc)
        return results

    # ------------------------------------------------------------------ 감지

    def _register(self, rows_by_date, announce):
        """조회 결과를 상태에 반영하고, 새로 생긴 회차를 알린다."""
        new_rows = []
        for rows in rows_by_date.values():
            for row in rows:
                key = showtime_key(row)
                if key not in self.known_keys:
                    self.known_keys.add(key)
                    new_rows.append(row)

        if not new_rows or not announce:
            return

        if len(new_rows) > BULK_ALERT_THRESHOLD:
            log.info("[%s] 신규 회차 %s개 (대량 변경, 요약 알림)", self.name_, len(new_rows))
            dates = sorted({row.get("scnYmd", "") for row in new_rows})
            self.notifier.send_text(
                f"🚨 **{self.name_}** 신규 회차 {len(new_rows)}개 감지 "
                f"({len(dates)}개 일자). {self.booking_link(dates[0])}"
            )
            return

        for group in self._group(new_rows):
            log.info(
                "[%s] 예매 오픈: %s %s %s",
                self.name_,
                group["date"],
                group["movie"],
                ",".join(group["times"]),
            )
        self.notifier.send_open_alert(self.name_, self._group(new_rows))

    def _group(self, rows):
        """(일자, 영화) 단위로 묶어 알림 가독성을 높인다."""
        buckets = {}
        for row in rows:
            movie = row.get("expoProdNm") or row.get("prodNm") or row.get("movNm") or "?"
            scn_ymd = row.get("scnYmd", "")
            bucket = buckets.setdefault(
                (scn_ymd, movie),
                {
                    "date": scn_ymd,
                    "movie": movie,
                    "screen": row.get("expoScnsNm") or row.get("scnsNm") or "-",
                    "seats": row.get("stcnt") or "-",
                    "url": self.booking_link(scn_ymd),
                    "times": [],
                },
            )
            bucket["times"].append(format_time(row.get("scnsrtTm")))

        groups = sorted(buckets.values(), key=lambda g: (g["date"], g["movie"]))
        for group in groups:
            group["times"] = sorted(set(group["times"]))
        return groups

    def _prune_past(self):
        """지난 날짜의 상태를 정리해 state.json이 무한히 커지지 않도록 한다."""
        today = date.today().strftime("%Y%m%d")
        self.known_dates = {d for d in self.known_dates if d >= today}
        self.known_keys = {k for k in self.known_keys if k.split("|", 1)[0] >= today}

    # ------------------------------------------------------------------ 실행

    def booking_link(self, scn_ymd):
        return booking_url(self.site_no, scn_ymd, self.site_nm)

    def bootstrap(self):
        saved = self.state.get(self.name_)

        # 예매 딥링크에는 극장명이 필요하다. 설정에 없으면 API로 조회한다.
        if not self.site_nm:
            try:
                self.site_nm = self.api.site_name(self.site_no)
            except CgvApiError as exc:
                log.warning("[%s] 극장명 조회 실패: %s", self.name_, exc)

        dates = sorted(set(self.api.open_dates(self.site_no)))
        self.known_dates = set(dates)

        if saved:
            self.known_keys = set(saved.get("keys", []))
            self._prune_past()
            # 저장된 상태가 있으면 이후 변경분부터 알린다.
            self._register(self._fetch_dates(dates), announce=True)
        else:
            # 최초 실행은 기준선만 만든다. 알림을 쏟아내지 않기 위함이다.
            self._register(self._fetch_dates(dates), announce=False)
            log.info(
                "[%s] 기준선 생성 완료: 일자 %s개 / 회차 %s개",
                self.name_,
                len(self.known_dates),
                len(self.known_keys),
            )

        self._persist()

    def _persist(self):
        self.state.put(self.name_, self.known_dates, self.known_keys)

    def _tail_dates(self):
        return sorted(self.known_dates)[-PRIORITY_TAIL_DAYS:]

    def run(self):
        try:
            self.bootstrap()
        except Exception as exc:
            log.exception("[%s] 초기화 실패: %s", self.name_, exc)
            self.notifier.send_text(f"⚠️ **{self.name_}** 초기화 실패: {exc}")
            return

        next_fast = time.monotonic()
        next_sweep = time.monotonic() + jitter(self.settings["sweep_interval"])
        next_full = time.monotonic() + jitter(FULL_SWEEP_INTERVAL)
        failures = 0

        while not self._stop_event.is_set():
            try:
                now = time.monotonic()

                if now >= next_fast:
                    next_fast = now + jitter(self.settings["fast_interval"])
                    self._tick_dates()

                if now >= next_full:
                    next_full = now + jitter(FULL_SWEEP_INTERVAL)
                    next_sweep = now + jitter(self.settings["sweep_interval"])
                    self._register(self._fetch_dates(sorted(self.known_dates)), announce=True)
                    self._prune_past()
                    self._persist()
                elif now >= next_sweep:
                    next_sweep = now + jitter(self.settings["sweep_interval"])
                    self._register(self._fetch_dates(self._tail_dates()), announce=True)

                failures = 0
                # 정상 응답이 돌아왔으므로 차단 단계를 초기화한다.
                if self.block_count:
                    log.info("[%s] 접근이 정상으로 돌아왔습니다.", self.name_)
                    self.block_count = 0
            except CgvBlockedError as exc:
                # 차단은 우회하지 않는다. 요청을 멈추고 충분히 기다린 뒤 재개한다.
                cooldown = BLOCK_COOLDOWNS[min(self.block_count, len(BLOCK_COOLDOWNS) - 1)]
                self.block_count += 1
                log.error(
                    "[%s] CGV 접근 제한 감지: %s → %.0f분간 요청 중단",
                    self.name_,
                    exc,
                    cooldown / 60,
                )
                if self.block_count == 1:
                    self.notifier.send_text(
                        f"⚠️ **{self.name_}** CGV 접근이 제한되었습니다.\n"
                        f"{cooldown / 60:.0f}분간 요청을 멈추고 자동으로 다시 시도합니다.\n"
                        f"반복되면 `CGV_FAST_INTERVAL` 값을 늘려주세요.",
                        color=0xE74C3C,
                    )
                # 재개 직후 곧바로 다시 두드리지 않도록 다음 실행 시각도 미룬다.
                resume = time.monotonic() + cooldown
                next_fast = next_sweep = next_full = resume
                self._stop_event.wait(cooldown)
                continue
            except CgvApiError as exc:
                failures += 1
                log.warning("[%s] API 오류(%s회): %s", self.name_, failures, exc)
                # v1은 오류 시 os.execl로 프로세스를 통째로 재시작했다.
                # v2는 백오프 후 같은 루프를 계속 돌아 상태를 잃지 않는다.
                self._stop_event.wait(min(2 ** min(failures, 5), 30))
                continue
            except Exception as exc:
                failures += 1
                log.exception("[%s] 예기치 못한 오류: %s", self.name_, exc)
                self._stop_event.wait(min(2 ** min(failures, 5), 30))
                continue

            self._stop_event.wait(0.2)

    def _tick_dates(self):
        dates = set(self.api.open_dates(self.site_no))
        added = sorted(dates - self.known_dates)
        self.known_dates = dates
        if not added:
            return

        log.info("[%s] 신규 예매 일자: %s", self.name_, ", ".join(added))
        self.notifier.send_new_dates(self.name_, added, self.booking_link(added[0]))
        # 새 일자는 곧바로 회차를 확인한다. 여기가 가장 빠른 감지 경로다.
        self._register(self._fetch_dates(added), announce=True)
        self._persist()
