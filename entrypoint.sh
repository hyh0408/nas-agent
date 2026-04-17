#!/bin/sh
# root 로 시작 → 권한 조정 → agent 유저로 전환
chmod 666 /var/run/docker.sock 2>/dev/null || true
chown -R agent:agent /app/data /app/projects 2>/dev/null || true
exec gosu agent python -m bot.main
