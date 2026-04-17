FROM python:3.11-slim

# Install Docker CLI (공식 바이너리 직접 설치)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl git ca-certificates gnupg default-mysql-client && \
    # Docker CLI 바이너리 직접 다운로드
    curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.4.1.tgz | \
    tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    # Node.js 20 설치 (Claude Code CLI 필수)
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY executor/ ./executor/

# gosu 로 root → non-root 전환
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

# non-root 사용자 생성 (Claude CLI 가 root 에서 --dangerously-skip-permissions 차단)
RUN groupadd -r agent && useradd -r -g agent -m -d /home/agent agent

COPY entrypoint.sh /entrypoint.sh

EXPOSE 9100

# root 로 시작 → entrypoint 에서 소켓 권한 조정 후 agent 로 전환
ENTRYPOINT ["/entrypoint.sh"]
