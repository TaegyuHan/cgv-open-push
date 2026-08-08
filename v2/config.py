"""v2 설정. 값은 환경변수로 덮어쓸 수 있고, 감시 대상은 targets.json으로 교체 가능하다."""

import json
import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent


def _float_env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 오픈 알림에 붙일 멘션. 예: "@everyone" 또는 "<@&역할ID>"
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()

# 예매 가능 일자 폴링 주기(초). 응답이 ~700B로 가볍지만, 차단을 피하기 위해
# 기본값을 보수적으로 잡는다. 오픈이 임박했다고 판단될 때만 낮추는 것을 권한다.
FAST_INTERVAL = _float_env("CGV_FAST_INTERVAL", 5.0)

# 회차 스윕 주기(초). 이미 열려 있던 날짜에 회차가 추가되는 경우를 잡는다.
SWEEP_INTERVAL = _float_env("CGV_SWEEP_INTERVAL", 60.0)

# 스윕 동시 요청 수. 높이면 순간 요청이 몰려 차단 위험이 커진다.
SWEEP_CONCURRENCY = _int_env("CGV_SWEEP_CONCURRENCY", 3)

# 상태 저장 경로. 재시작 시 이미 알린 회차를 다시 알리지 않기 위해 사용한다.
STATE_PATH = pathlib.Path(os.environ.get("CGV_STATE_PATH", BASE_DIR / "state.json"))

LOG_PATH = pathlib.Path(os.environ.get("CGV_LOG_PATH", BASE_DIR / "cgv-open-push-v2.log"))

HTTP_TIMEOUT = _float_env("CGV_HTTP_TIMEOUT", 8.0)

# 하루 1회 생존 신고를 보낼 시각(0~23시). -1이면 보내지 않는다.
# CGV는 날짜를 매일 미는 것이 아니라 며칠~일주일 단위로 뭉텅이 오픈하므로
# 며칠간 알림이 없는 것이 정상이다. 감시가 죽은 것과 구분하기 위해 필요하다.
HEARTBEAT_HOUR = _int_env("CGV_HEARTBEAT_HOUR", 9)

# 감시 대상 기본값. 용산아이파크몰 IMAX(용아맥)가 주 목표다.
#   site_no  : 극장 코드
#   scns_no  : 상영관 번호. 지정하면 응답 크기가 크게 줄어든다.
#   grade_cd : 특별관 등급 코드(01 일반 / 02 4DX / 03 IMAX / 04 SCREENX)
DEFAULT_TARGETS = [
    {
        "name": "용아맥 (용산아이파크몰 IMAX)",
        "site_no": "0013",
        "site_nm": "용산아이파크몰",
        "scns_no": "018",
        "grade_cd": "03",
    },
]


def load_targets():
    path = BASE_DIR / "targets.json"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            targets = json.load(f)
        if targets:
            return targets
    return DEFAULT_TARGETS
