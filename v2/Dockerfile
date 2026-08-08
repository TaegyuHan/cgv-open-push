FROM python:3.12-slim

LABEL name="cgv-open-push-v2"

# TZ 환경변수만으로는 부족해 tzdata를 설치한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 재시작 후에도 중복 알림이 발생하지 않도록 상태 파일은 볼륨에 둔다.
ENV CGV_STATE_PATH=/data/state.json
VOLUME ["/data"]

ENV DISCORD_WEBHOOK_URL=""
ENV DISCORD_MENTION=""
ENV CGV_FAST_INTERVAL="5"
ENV CGV_SWEEP_INTERVAL="30"
ENV PYTHONUNBUFFERED="1"

# 로그 시각과 날짜 계산을 한국 시간 기준으로 맞춘다.
ENV TZ="Asia/Seoul"

ENTRYPOINT ["python", "main.py"]
CMD ["run"]
