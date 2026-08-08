"""Discord 웹훅 알림 전송."""

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

# Discord 임베드는 메시지당 10개까지만 허용된다.
MAX_EMBEDS_PER_MESSAGE = 10

COLOR_OPEN = 0x2ECC71
COLOR_INFO = 0x3498DB
COLOR_ERROR = 0xE74C3C


class DiscordNotifier:
    def __init__(self, webhook_url, mention="", timeout=10.0):
        self.webhook_url = webhook_url
        self.mention = mention
        self.timeout = timeout
        self.session = requests.Session()
        self.enabled = bool(webhook_url)
        if not self.enabled:
            log.warning("DISCORD_WEBHOOK_URL이 설정되지 않아 알림은 콘솔에만 출력됩니다.")

    def _post(self, payload):
        if not self.enabled:
            log.info("[알림 미전송] %s", json.dumps(payload, ensure_ascii=False)[:500])
            return

        for attempt in range(4):
            try:
                response = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.error("Discord 전송 실패(%s/4): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                # Discord가 알려주는 대기 시간을 그대로 존중한다.
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    retry_after = 1.0
                time.sleep(min(retry_after, 10.0))
                continue

            if response.status_code >= 400:
                log.error("Discord 전송 실패 HTTP %s: %s", response.status_code, response.text[:300])
                if response.status_code < 500:
                    return
                time.sleep(2 ** attempt)
                continue
            return

        log.error("Discord 전송을 최종 실패했습니다.")

    def send_open_alert(self, target_name, groups):
        """예매 오픈 알림 전송.

        groups: [{"date", "movie", "times", "screen", "seats", "url"}, ...]
        """
        embeds = []
        for group in groups:
            times = ", ".join(group["times"])
            embeds.append(
                {
                    "title": f"🎬 {group['movie']}",
                    "url": group["url"],
                    "color": COLOR_OPEN,
                    "description": (
                        f"### [👉 지금 바로 예매하기]({group['url']})\n"
                        f"-# 페이지가 영화별로 묶여 있습니다. "
                        f"**{group['movie']}** → **{group['screen']}** 을 찾으세요 "
                        f"(`특별관` 탭 또는 Ctrl+F 검색)"
                    ),
                    "fields": [
                        {"name": "날짜", "value": _format_date(group["date"]), "inline": True},
                        {"name": "상영관", "value": group["screen"], "inline": True},
                        {"name": "총 좌석", "value": f"{group['seats']}석", "inline": True},
                        {"name": f"회차 {len(group['times'])}개", "value": times, "inline": False},
                    ],
                }
            )

        header = f"🚨 **예매 오픈 감지** — {target_name}"
        if self.mention:
            header = f"{self.mention} {header}"

        # 임베드 10개 단위로 잘라 전송한다.
        for index in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
            chunk = embeds[index : index + MAX_EMBEDS_PER_MESSAGE]
            self._post({"content": header if index == 0 else None, "embeds": chunk})

    def send_new_dates(self, target_name, dates, url):
        listed = ", ".join(_format_date(d) for d in dates)
        self._post(
            {
                "embeds": [
                    {
                        "title": f"📅 {target_name} 예매 가능 일자 추가",
                        "url": url,
                        "description": f"{listed}\n\n[👉 예매하러 가기]({url})",
                        "color": COLOR_INFO,
                    }
                ]
            }
        )

    def send_text(self, message, color=COLOR_INFO):
        self._post({"embeds": [{"description": message, "color": color}]})

    def send_heartbeat(self, snapshots):
        """하루 1회 생존 신고. 새 회차가 없어도 감시가 살아 있음을 알린다."""
        import time as _time

        lines = []
        healthy = True
        for snap in snapshots:
            if snap["blocked"]:
                healthy = False
                status = "⛔ 접근 제한됨"
            elif snap["last_ok_at"] and _time.time() - snap["last_ok_at"] > 600:
                healthy = False
                status = "⚠️ 최근 응답 없음"
            else:
                status = "✅ 정상"
            span = (
                f"{_format_date(snap['first'])} ~ {_format_date(snap['last'])}"
                if snap["first"]
                else "-"
            )
            lines.append(
                f"**{snap['name']}** {status}\n"
                f"　예매 가능 일자 **{snap['dates']}일** ({span})\n"
                f"　감시 중인 회차 **{snap['showtimes']}개**"
            )

        body = "\n\n".join(lines)
        self._post(
            {
                "embeds": [
                    {
                        "title": "🫀 감시 정상 동작 중",
                        "description": (
                            f"{body}\n\n"
                            "새 회차가 열리면 즉시 알림이 갑니다. "
                            "이 메시지는 하루 1회 생존 확인용입니다."
                        ),
                        "color": COLOR_INFO if healthy else 0xE74C3C,
                    }
                ]
            }
        )


def _format_date(ymd):
    if len(ymd) != 8:
        return ymd
    weekday = "월화수목금토일"
    try:
        import datetime

        date = datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        return f"{date.month}/{date.day}({weekday[date.weekday()]})"
    except ValueError:
        return ymd
