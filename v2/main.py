"""CGV 예매 오픈 알리미 v2.

사용법:
    python main.py run           감시 시작 (기본)
    python main.py check         현재 감시 대상의 회차를 1회 조회해 출력
    python main.py sites [검색어] 극장 코드 조회
    python main.py screens 0013  해당 극장의 상영관/특별관 코드 조회
    python main.py test-notify   Discord 웹훅 연결 확인
"""

import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import config
from cgv_api import CgvApi, CgvApiError
from notifier import DiscordNotifier
from watcher import StateStore, TargetWatcher, format_time

log = logging.getLogger("cgv-open-push")


def setup_logging():
    handlers = [
        RotatingFileHandler(
            config.LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        handlers=handlers,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_settings():
    return {
        "fast_interval": config.FAST_INTERVAL,
        "sweep_interval": config.SWEEP_INTERVAL,
        "sweep_concurrency": config.SWEEP_CONCURRENCY,
        "http_timeout": config.HTTP_TIMEOUT,
    }


def cmd_run():
    targets = config.load_targets()
    notifier = DiscordNotifier(config.DISCORD_WEBHOOK_URL, config.DISCORD_MENTION)
    state = StateStore(str(config.STATE_PATH))
    settings = build_settings()

    log.info(
        "감시 시작: %s / 일자폴링 %ss / 회차스윕 %ss",
        ", ".join(t["name"] for t in targets),
        settings["fast_interval"],
        settings["sweep_interval"],
    )
    notifier.send_text(
        "✅ CGV 예매 오픈 알리미 v2 시작\n감시 대상: "
        + ", ".join(t["name"] for t in targets)
    )

    watchers = [TargetWatcher(t, notifier, state, settings) for t in targets]
    for watcher in watchers:
        watcher.start()

    stopping = threading.Event()

    def handle_signal(signum, _frame):
        log.info("종료 신호 수신 (%s)", signal.Signals(signum).name)
        stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, AttributeError):
            pass

    try:
        while any(w.is_alive() for w in watchers) and not stopping.is_set():
            stopping.wait(1)
    except KeyboardInterrupt:
        log.info("종료 요청됨")
    finally:
        for watcher in watchers:
            watcher.stop()
        for watcher in watchers:
            watcher.join(timeout=15)
        notifier.send_text("⏹️ CGV 예매 오픈 알리미 v2 중지")
    return 0


def cmd_check():
    api = CgvApi(timeout=config.HTTP_TIMEOUT)
    for target in config.load_targets():
        site_no = target["site_no"]
        dates = api.open_dates(site_no)
        last = dates[-1] if dates else "-"
        print(f"\n=== {target['name']} (siteNo={site_no})")
        print(f"예매 가능 일자 {len(dates)}개: {dates[0] if dates else '-'} ~ {last}")

        found = 0
        for scn_ymd in dates:
            rows = [
                row
                for row in api.schedules(site_no, scn_ymd, target.get("scns_no", ""))
                if not target.get("grade_cd")
                or str(row.get("tcscnsGradCd")) == target["grade_cd"]
            ]
            if not rows:
                continue
            found += len(rows)
            movies = sorted({r.get("expoProdNm") or r.get("prodNm") or "?" for r in rows})
            times = ", ".join(sorted(format_time(r.get("scnsrtTm")) for r in rows))
            print(f"  {scn_ymd}  {' / '.join(movies)}")
            print(f"            {times}")
        print(f"총 회차: {found}개")
    return 0


def cmd_sites(keyword=""):
    api = CgvApi(timeout=config.HTTP_TIMEOUT)
    for site in api.sites():
        name = site.get("siteNm", "")
        if keyword and keyword not in name:
            continue
        print(f"{site.get('siteNo')}  {name}")
    return 0


def cmd_screens(site_no):
    api = CgvApi(timeout=config.HTTP_TIMEOUT)
    dates = api.open_dates(site_no)
    if not dates:
        print("예매 가능한 일자가 없습니다.")
        return 1

    seen = {}
    # 특별관은 매일 편성되지 않을 수 있어 앞쪽 며칠을 훑는다.
    for scn_ymd in dates[:5]:
        for row in api.schedules(site_no, scn_ymd):
            key = (row.get("tcscnsGradCd"), row.get("scnsNo"))
            seen.setdefault(
                key,
                (row.get("tcscnsGradNm"), row.get("scnsNo"), row.get("scnsNm")),
            )

    print(f"siteNo={site_no} 상영관 목록")
    print(f"{'grade_cd':10}{'scns_no':10}{'등급':10}상영관")
    for (grade_cd, scns_no), (grade_nm, _, scns_nm) in sorted(seen.items()):
        print(f"{grade_cd or '-':10}{scns_no or '-':10}{grade_nm or '-':10}{scns_nm or '-'}")
    return 0


def cmd_test_notify():
    notifier = DiscordNotifier(config.DISCORD_WEBHOOK_URL, config.DISCORD_MENTION)
    if not notifier.enabled:
        print("DISCORD_WEBHOOK_URL 환경변수가 비어 있습니다.")
        return 1
    notifier.send_text("🔔 CGV 예매 오픈 알리미 v2 웹훅 테스트")
    print("전송했습니다. Discord 채널을 확인하세요.")
    return 0


def main(argv):
    setup_logging()
    command = argv[1] if len(argv) > 1 else "run"
    args = argv[2:]

    try:
        if command == "run":
            return cmd_run()
        if command == "check":
            return cmd_check()
        if command == "sites":
            return cmd_sites(args[0] if args else "")
        if command == "screens":
            if not args:
                print("사용법: python main.py screens <siteNo>")
                return 1
            return cmd_screens(args[0])
        if command == "test-notify":
            return cmd_test_notify()
    except CgvApiError as exc:
        log.error("CGV API 오류: %s", exc)
        return 1

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
