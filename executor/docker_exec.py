"""Docker 명령 실행기 - 호스트 Docker 소켓을 통해 제어"""

import asyncio
import subprocess


async def run_cmd(*args: str, timeout: int = 60) -> str:
    """쉘 명령을 비동기로 실행하고 결과를 반환한다."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return f"명령을 찾을 수 없습니다: {args[0]}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "명령 실행 시간 초과 (timeout)"

    output = stdout.decode().strip()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        return f"오류 (exit {proc.returncode}):\n{err}"
    return output or "(출력 없음)"


async def container_status() -> str:
    """실행 중인 컨테이너 목록을 반환한다."""
    return await run_cmd(
        "docker", "ps",
        "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    )


async def container_logs(name: str, tail: int = 30) -> str:
    """컨테이너 로그를 반환한다."""
    return await run_cmd("docker", "logs", "--tail", str(tail), name)


async def container_stop(name: str) -> str:
    return await run_cmd("docker", "stop", name)


async def container_restart(name: str) -> str:
    return await run_cmd("docker", "restart", name)


async def deploy_project(project_name: str, projects_dir: str) -> str:
    """docker-compose.yml이 있는 프로젝트를 배포한다."""
    project_path = f"{projects_dir}/{project_name}"
    compose_file = f"{project_path}/docker-compose.yml"

    # compose 파일 존재 확인
    result = subprocess.run(["test", "-f", compose_file], capture_output=True)
    if result.returncode != 0:
        return f"프로젝트 '{project_name}'에 docker-compose.yml이 없습니다."

    return await run_cmd(
        "docker", "compose", "-f", compose_file, "up", "-d", "--build",
        timeout=300,
    )


async def list_projects(projects_dir: str) -> str:
    """프로젝트 디렉터리 목록을 반환한다."""
    return await run_cmd("ls", "-1", projects_dir)


async def system_status() -> str:
    """NAS 호스트의 CPU, 메모리, 디스크, 컨테이너 요약을 반환한다."""
    import shutil

    # 메모리 (/proc/meminfo는 호스트 값을 반영)
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            mem[key.strip()] = int(rest.strip().split()[0])  # kB
    total_gb = mem["MemTotal"] / 1024 / 1024
    avail_gb = mem.get("MemAvailable", mem["MemFree"]) / 1024 / 1024
    used_gb = total_gb - avail_gb
    mem_pct = used_gb / total_gb * 100

    # 로드 평균
    with open("/proc/loadavg") as f:
        load = f.read().split()[:3]

    # CPU 코어 수
    with open("/proc/cpuinfo") as f:
        cores = sum(1 for line in f if line.startswith("processor"))

    # 디스크 (프로젝트 볼륨)
    du = shutil.disk_usage("/app/projects")
    disk_total = du.total / 1024**3
    disk_used = du.used / 1024**3
    disk_pct = du.used / du.total * 100

    # 실행 중 컨테이너 수
    containers = await run_cmd("docker", "ps", "-q")
    container_count = len(containers.splitlines()) if containers and "(출력 없음)" not in containers else 0

    return (
        f"🖥  NAS 상태\n"
        f"─────────────────\n"
        f"CPU  : {cores} cores, load {load[0]} / {load[1]} / {load[2]}\n"
        f"MEM  : {used_gb:.1f} / {total_gb:.1f} GB ({mem_pct:.0f}%)\n"
        f"DISK : {disk_used:.1f} / {disk_total:.1f} GB ({disk_pct:.0f}%)\n"
        f"CONT : {container_count} running"
    )
