"""
텔레그램 방의 PDF 파일을 로컬 폴더(웹사이트 배포 폴더)로 내려받는 스크립트.

동작 방식 (v2: 장시간 리스너 구조)
---------------------------------
- 봇 계정이 아니라 "내 텔레그램 계정"으로 로그인합니다 (Telethon 사용).
  이미 그 방의 멤버로 초대되어 있다면 관리자 권한 없이도 방에 올라온
  모든 메시지/파일을 그대로 읽을 수 있습니다.
- 실행되면 먼저 "캐치업 스캔"으로 지난 실행 이후 놓친 메시지를 한 바퀴 훑어서
  받고, 그다음부터는 텔레그램 새 메시지를 실시간 이벤트로 받아서 그 자리에서
  바로 다운로드합니다 (30분마다 새로 접속하는 방식이 아니라 한 번 붙으면
  계속 붙어있는 방식).
- GitHub Actions 호스티드 러너는 Job 하나당 최대 6시간까지만 허용되므로,
  MAX_RUNTIME_SECONDS(기본 5시간 30분)가 지나면 스스로 정상 종료합니다.
- **종료 직전에 GitHub API를 직접 호출해서 "다음 실행"을 스스로 예약합니다**
  (workflow_dispatch). sync.yml의 cron(schedule)은 GitHub 인프라 부하에 따라
  지연되거나 아예 드롭될 수 있다고 공식 문서에 나와 있어서, 체인을 이어가는
  주된 수단으로 쓰지 않습니다. cron은 이 체인이 어쩌다 끊겼을 때를 대비한
  보험으로만 sync.yml에 남아있습니다 (기본 6시간 주기).
- 첨부파일과 함께 올라온 메시지 본문(캡션)에서 "년도 / 강사 / 과목" 같은
  메타데이터와 제목을 최대한 파싱해서 함께 기록합니다.
- PDF 첫 페이지를 이미지로 렌더링해서 썸네일로 저장합니다 (sites/thumbnails/).
- 2026-09-04 이후에 올라온 메시지만 대상으로 하며, 그보다 오래된 메시지가
  나오면 캐치업 스캔을 그 자리에서 중단합니다.
- 파일 크기가 150MB를 초과하는 PDF는 건너뜁니다.
- sites/manifest.json에 파일 목록(이름, 크기, 날짜, 메타데이터, 썸네일 경로)을
  기록합니다. 웹사이트(index.html)는 이 manifest.json을 읽어서 목록을 보여줍니다.
- 이미 내려받은 파일(manifest에 message_id 존재)은 건너뛰어 중복 다운로드하지 않습니다.
- Firebase, Firestore, 외부 클라우드 API를 전혀 호출하지 않습니다. (GitHub 자체
  API 호출은 "다음 실행 예약" 용도로만 사용합니다.)

로그인 방식
-----------
- TELEGRAM_SESSION 환경변수(StringSession 문자열)가 있으면 그걸로 로그인합니다.
  (GitHub Actions 등 자동화 환경에서 사용, 인증코드 입력 불필요)
- 없으면 로컬의 telegram_session.session 파일로 로그인합니다.
  (최초 실행 시에만 전화번호/인증코드 입력 필요, 이후엔 파일로 재인증 불필요)

주의: 이 세션은 한 번에 "한 곳"에서만 붙어있어야 합니다. 같은 세션으로 동시에
두 프로세스가(예: 겹치는 실행 두 개, 또는 로컬 테스트 + Actions 동시 실행)
접속하면 텔레그램이 AuthKeyDuplicatedError로 세션 자체를 폐기합니다.
sync.yml의 concurrency 설정이 "동시에 두 실행이 못 붙는 것"을 보장해줍니다.
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename

load_dotenv()

# ---- 환경 변수 ---------------------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]

_chat_raw = os.environ["TELEGRAM_CHAT"]  # 채널/그룹 username(@없이) 또는 숫자 ID
CHAT = int(_chat_raw) if _chat_raw.lstrip("-").isdigit() else _chat_raw

SESSION_STRING = os.environ.get("TELEGRAM_SESSION", "").strip() or None  # GitHub Secrets 등, 없으면 로컬 파일 세션 사용

# ---- 동기화 조건 --------------------------------------------------------
# 이 날짜 이후에 올라온 메시지만 동기화 (하드코딩: 2026-09-04부터)
CUTOFF = datetime(2026, 9, 4, tzinfo=timezone.utc)

# 이 크기(바이트)를 초과하는 PDF는 건너뜀 (150MB)
MAX_SIZE_BYTES = 150 * 1024 * 1024

# 썸네일 이미지의 가로 폭 (px). PDF 첫 페이지를 이 폭에 맞춰 렌더링함.
THUMBNAIL_WIDTH = 400

# 한 프로세스가 텔레그램에 붙어있는 최대 시간(초). GitHub Actions 호스티드 러너의
# 절대 상한(6시간)보다 넉넉하게 여유를 두고 스스로 정상 종료한 뒤, 다음 실행을
# 직접 예약한다 (trigger_next_run 참고). sync.yml의 step/job timeout-minutes도
# 이 값보다 커야 한다.
MAX_RUNTIME_SECONDS = 5 * 60 * 60 + 30 * 60  # 5시간 30분

SITE_DIR = Path("sites")
FILES_DIR = SITE_DIR / "files"
THUMBS_DIR = SITE_DIR / "thumbnails"
MANIFEST_PATH = SITE_DIR / "manifest.json"

FILES_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# GitHub Actions 안에서 실행 중일 때만, 파일 하나 받을 때마다 즉시 git commit + push.
# (로컬에서 그냥 테스트 삼아 돌릴 때는 자동으로 커밋/푸시하지 않도록 방지)
AUTO_GIT_PUSH = os.environ.get("GITHUB_ACTIONS") == "true"

# ---- 다음 실행을 스스로 예약하기 위한 GitHub API 설정 ----------------------
# sync.yml에서 permissions.actions=write 로 발급된 기본 GITHUB_TOKEN을
# GH_DISPATCH_TOKEN 이름으로 주입해준다. (레포/워크플로 이름은 Actions가
# 자동으로 GITHUB_REPOSITORY 환경변수에 넣어줌)
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo"
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main")
GITHUB_WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "sync.yml")
GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN")

# ---- 캡션 메타데이터 파싱 ------------------------------------------------
# 텔레그램 메시지 본문(캡션)에 아래처럼 붙어있는 경우가 많음:
#
#   극어 부록 매체 N제
#   년도 : 2027
#   강사 : 방동진
#   과목 : 국어
#   @yubin_MPGA
#
# 하지만 모든 파일이 이 형식을 따르는 건 아니므로, 각 라벨(년도/강사/과목)은
# 줄 단위로 독립적으로 찾고, 없으면 그냥 비워둔다. 제목은 라벨이 아니고
# @로 시작하지 않는 첫 줄로 추정하고, 그마저 없으면 나중에 파일명으로 대체한다.
_LABEL_PATTERNS = {
    "year": re.compile(r"^[^\w가-힣]*년도\s*[:：]\s*(.+)$"),
    "instructor": re.compile(r"^[^\w가-힣]*강사\s*[:：]\s*(.+)$"),
    "subject": re.compile(r"^[^\w가-힣]*과목\s*[:：]\s*(.+)$"),
}


def parse_caption(caption: str | None) -> dict:
    """메시지 캡션에서 title/year/instructor/subject를 최대한 뽑아낸다.
    형식이 다르거나 캡션이 아예 없어도 에러 없이 빈 값으로 채워 반환한다."""
    result = {"title": None, "year": None, "instructor": None, "subject": None}
    if not caption:
        return result

    for raw_line in caption.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        matched = False
        for key, pattern in _LABEL_PATTERNS.items():
            m = pattern.match(line)
            if m and result[key] is None:
                result[key] = m.group(1).strip()
                matched = True
                break
        if matched:
            continue

        if line.startswith("@"):
            continue  # 텔레그램 아이디 언급 줄은 제목 후보에서 제외

        if result["title"] is None:
            result["title"] = line

    return result


def compute_sha256(path: Path) -> str:
    """파일 내용의 SHA-256 해시. 같은 내용의 파일(이름은 달라도)을 잡아내는 데 사용."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_thumbnail(pdf_path: Path, thumb_path: Path) -> bool:
    """PDF 첫 페이지를 이미지로 렌더링해서 thumb_path에 저장. 성공하면 True."""
    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            if page.rect.width <= 0:
                return False
            zoom = THUMBNAIL_WIDTH / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            pix.save(thumb_path)
        return True
    except Exception as e:
        print(f"    [썸네일 생성 실패] {pdf_path.name}: {e}", flush=True)
        return False


def _run_git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _has_unpushed_commits() -> bool:
    """현재 브랜치가 원격(업스트림)보다 앞서있는 커밋이 있는지 확인."""
    result = _run_git("rev-list", "@{u}..HEAD", "--count")
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def _push_with_retry(max_retries: int = 5) -> bool:
    """현재 HEAD를 push. 실패하면(다른 실행/수동 편집과 충돌 등) pull --rebase로
    원격 변경사항을 받아와서 재시도한다. 이게 없으면, push가 실패한 커밋은
    이 컨테이너가 폐기되는 순간 그대로 유실되고 -> 그 파일이 "다운로드됨"으로
    기록되지 않은 채로 다음 실행에서 재다운로드되는 문제로 이어진다."""
    for attempt in range(1, max_retries + 1):
        push = _run_git("push")
        if push.returncode == 0:
            print("    [git push 성공]", flush=True)
            return True

        print(f"    [git push 실패 (시도 {attempt}/{max_retries})] {push.stderr.strip()}", flush=True)

        pull = _run_git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            print(f"    [git pull --rebase 실패] {pull.stderr.strip()}", flush=True)

        time.sleep(min(2 ** attempt, 20))

    print(
        f"    [git push 최종 실패] {max_retries}번 재시도했지만 실패했습니다. "
        "이 커밋은 로컬에만 남아있어 이번 실행 종료 시 유실될 수 있습니다.",
        flush=True,
    )
    return False


def git_commit_and_push(commit_message: str) -> None:
    """sites/ 폴더의 변경사항을 즉시 커밋하고, 실패해도 재시도하며 push한다.
    변경사항이 없으면(이미 커밋된 상태 등) 커밋은 건너뛰지만, 혹시 이전에
    push만 실패해서 로컬에 밀린 커밋이 남아있다면 그것까지 함께 재시도한다."""
    _run_git("add", "sites/")

    diff = _run_git("diff", "--staged", "--quiet")
    if diff.returncode != 0:
        commit = _run_git("commit", "-m", commit_message)
        print(f"    [git commit] {commit.stdout.strip()}{commit.stderr.strip()}", flush=True)

    if _has_unpushed_commits():
        _push_with_retry()


def is_pdf(message) -> tuple[bool, str | None]:
    """메시지가 PDF 첨부파일을 담고 있으면 (True, 파일명)을 반환."""
    if not message.document:
        return False, None
    mime_ok = message.document.mime_type == "application/pdf"
    filename = None
    for attr in message.document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
    name_ok = filename is not None and filename.lower().endswith(".pdf")
    if mime_ok or name_ok:
        return True, filename or f"document_{message.id}.pdf"
    return False, None


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"files": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def trigger_next_run() -> None:
    """다음 실행을 GitHub API로 직접 예약한다 (workflow_dispatch).

    cron(schedule)은 GitHub 인프라 부하가 높을 때 지연되거나 아예 드롭될 수
    있다고 공식 문서에 나와 있어서(특히 정시/30분 등 인기 시간대), 체인을
    이어가는 주된 수단으로 쓰지 않는다. API를 직접 호출하는 workflow_dispatch는
    그 "best-effort 스케줄 큐"를 거치지 않아서 훨씬 안정적이다.

    이 호출이 실패해도(네트워크 문제, 토큰 문제 등) 죽지 않는다 - sync.yml에
    남겨둔 6시간 주기 schedule이 최후의 보험으로 다시 살려준다.
    """
    if not AUTO_GIT_PUSH:
        print("[다음 실행 예약 건너뜀] GitHub Actions 환경이 아님 (로컬 테스트 등)", flush=True)
        return
    if not (GITHUB_REPOSITORY and GH_DISPATCH_TOKEN):
        print(
            "[다음 실행 예약 건너뜀] GITHUB_REPOSITORY 또는 GH_DISPATCH_TOKEN이 없음. "
            "sync.yml의 permissions.actions=write 와 env.GH_DISPATCH_TOKEN 설정을 확인하세요.",
            flush=True,
        )
        return

    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/"
        f"workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    )
    body = json.dumps({"ref": GITHUB_REF_NAME}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[다음 실행 예약 완료] HTTP {resp.status}", flush=True)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        print(f"[다음 실행 예약 실패] HTTP {e.code}: {detail}", flush=True)
    except Exception as e:
        print(f"[다음 실행 예약 실패] {e}", flush=True)


async def sync():
    manifest = load_manifest()
    manifest.setdefault("duplicate_message_ids", [])

    known_message_ids = {f["message_id"] for f in manifest["files"]}
    known_message_ids |= set(manifest["duplicate_message_ids"])

    # 1단계(다운로드 전) 중복 검사용: 파일명+용량이 완전히 같으면 십중팔구 재업로드.
    known_name_size = {(f["filename"].lower(), f["size_bytes"]) for f in manifest["files"]}
    # 2단계(다운로드 후) 중복 검사용: 이름이 달라도 내용이 같은 파일을 잡아냄.
    known_hashes = {f["sha256"]: f for f in manifest["files"] if f.get("sha256")}

    if AUTO_GIT_PUSH:
        # 파일마다 즉시 커밋하려면 git 사용자 정보가 필요.
        _run_git("config", "user.name", "github-actions[bot]")
        _run_git("config", "user.email", "github-actions[bot]@users.noreply.github.com")

    print(f"세션 문자열 존재 여부: {bool(SESSION_STRING)}, 길이: {len(SESSION_STRING) if SESSION_STRING else 0}", flush=True)

    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    else:
        client = TelegramClient("telegram_session", API_ID, API_HASH)  # 로컬 fallback

    print("연결 시도...", flush=True)
    await asyncio.wait_for(client.connect(), timeout=30)
    print("연결 성공. 인증 상태 확인 중...", flush=True)

    authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=30)
    print(f"인증 여부: {authorized}", flush=True)

    if not authorized:
        # 세션이 유효하지 않으면 여기서 즉시 실패시킴.
        # (client.start()에 맡기면 인증코드 입력을 무한정 기다려서
        #  GitHub Actions에서는 그냥 멈춰있는 것처럼 보이게 됨)
        raise RuntimeError(
            "TELEGRAM_SESSION이 유효하지 않습니다. "
            "로컬에서 generate_session.py를 다시 실행해 새 세션 문자열을 뽑고 "
            "GitHub Secret의 TELEGRAM_SESSION 값을 갱신하세요."
        )

    print("로그인 성공!", flush=True)

    print(f"CHAT 값: {CHAT!r} (타입: {type(CHAT).__name__})", flush=True)
    print("entity 조회 시도...", flush=True)
    try:
        entity = await asyncio.wait_for(client.get_entity(CHAT), timeout=30)
    except (ValueError, asyncio.TimeoutError) as e:
        # 숫자 ID인 경우, 새로 만든 세션에는 이 대화방의 entity 캐시가 없어서
        # get_entity가 바로 못 찾는 경우가 흔함. dialogs 목록을 먼저 불러와
        # 캐시를 채운 뒤 다시 시도.
        print(f"entity 바로 조회 실패({e}), dialogs 목록으로 재시도...", flush=True)
        dialogs = await asyncio.wait_for(client.get_dialogs(), timeout=60)
        print(f"대화방 {len(dialogs)}개 로드됨", flush=True)
        entity = None
        for d in dialogs:
            if d.id == CHAT or str(d.id) == str(CHAT):
                entity = d.entity
                break
        if entity is None:
            raise RuntimeError(
                f"TELEGRAM_CHAT={CHAT!r} 에 해당하는 대화방을 찾을 수 없습니다. "
                "list_chats.py로 정확한 ID를 다시 확인하세요."
            )
    print(f"entity 조회 성공: {getattr(entity, 'title', CHAT)}", flush=True)
    print(f"'{getattr(entity, 'title', CHAT)}' 방 감시 시작 (기준일: {CUTOFF.date()} 이후, {MAX_SIZE_BYTES // (1024*1024)}MB 이하)", flush=True)

    new_count = 0
    skipped_too_big = 0
    checked_count = 0
    start_time = time.monotonic()

    async def handle_pdf_message(message) -> bool:
        """새로 발견된 메시지 하나를 검사해서, PDF면 다운로드/썸네일/manifest/git까지
        전부 처리한다. 캐치업 스캔과 실시간 리스너가 이 함수 하나를 공유한다.
        새로 저장했으면 True, 아니면(PDF 아님/이미 알고 있음/중복/용량초과) False."""
        nonlocal new_count, skipped_too_big

        ok, filename = is_pdf(message)
        if not ok or message.id in known_message_ids:
            return False

        file_size = message.document.size
        if file_size > MAX_SIZE_BYTES:
            print(f"  건너뜀 (용량 초과 {file_size / (1024*1024):.1f}MB): {filename}", flush=True)
            skipped_too_big += 1
            return False

        # 1단계 중복 검사 (다운로드 전): 파일명+용량이 기존 파일과 완전히
        # 같으면 재업로드로 간주하고 다운로드 자체를 건너뜀 (대역폭 절약).
        if (filename.lower(), file_size) in known_name_size:
            print(f"  건너뜀 (파일명+용량이 동일한 기존 파일 있음, 중복으로 추정): {filename}", flush=True)
            manifest["duplicate_message_ids"].append(message.id)
            known_message_ids.add(message.id)
            save_manifest(manifest)
            if AUTO_GIT_PUSH:
                git_commit_and_push(f"chore: 중복 파일 스킵 - {filename} [skip ci]")
            return False

        # 파일명이 중복될 수 있으니 메시지 ID를 접두어로 붙여 저장
        safe_filename = f"{message.id}_{filename}"
        local_path = FILES_DIR / safe_filename

        print(f"  내려받는 중 ({file_size / (1024*1024):.1f}MB): {filename}", flush=True)

        last_pct = [-10]

        def _progress(current, total):
            pct = int(current / total * 100) if total else 0
            if pct - last_pct[0] >= 10:
                last_pct[0] = pct
                print(f"    ...{pct}% ({current / (1024*1024):.1f}/{total / (1024*1024):.1f}MB)", flush=True)

        await client.download_media(message, file=str(local_path), progress_callback=_progress)
        print(f"  완료: {filename}", flush=True)

        # 2단계 중복 검사 (다운로드 후): 파일명은 다르지만 내용이 완전히
        # 같은 경우(리네임된 재업로드)를 해시로 잡아냄.
        file_hash = compute_sha256(local_path)
        if file_hash in known_hashes:
            original = known_hashes[file_hash]
            print(f"  중복 파일 감지 (기존 '{original['filename']}'와 내용 동일), 저장하지 않고 삭제: {filename}", flush=True)
            local_path.unlink(missing_ok=True)
            manifest["duplicate_message_ids"].append(message.id)
            known_message_ids.add(message.id)
            save_manifest(manifest)
            if AUTO_GIT_PUSH:
                git_commit_and_push(f"chore: 중복 파일 스킵 - {filename} [skip ci]")
            return False

        # 캡션(메시지 본문)에서 년도/강사/과목/제목 파싱.
        meta = parse_caption(message.message)
        title = meta["title"] or Path(filename).stem

        # PDF 첫 페이지 썸네일 생성 (실패해도 목록 자체는 계속 진행)
        thumb_filename = f"{message.id}.png"
        thumb_path = THUMBS_DIR / thumb_filename
        thumbnail_ok = make_thumbnail(local_path, thumb_path)

        new_entry = {
            "message_id": message.id,
            "filename": filename,          # 사람이 보는 원래 파일명
            "stored_as": safe_filename,     # 실제 저장된 파일명 (다운로드 링크에 사용)
            "size_bytes": local_path.stat().st_size,
            "telegram_date": message.date.isoformat(),
            "title": title,
            "year": meta["year"],
            "instructor": meta["instructor"],
            "subject": meta["subject"],
            "thumbnail": f"thumbnails/{thumb_filename}" if thumbnail_ok else None,
            "sha256": file_hash,
        }
        manifest["files"].append(new_entry)
        new_count += 1
        known_message_ids.add(message.id)
        known_name_size.add((filename.lower(), new_entry["size_bytes"]))
        known_hashes[file_hash] = new_entry

        # 파일 하나 받을 때마다 바로 manifest를 저장.
        manifest["files"].sort(key=lambda f: f["telegram_date"], reverse=True)
        save_manifest(manifest)

        # 파일 하나 받을 때마다 바로 git commit + push까지 끝냄.
        if AUTO_GIT_PUSH:
            git_commit_and_push(f"chore: PDF 자동 동기화 - {filename} [skip ci]")

        return True

    # ---- 1) 캐치업 스캔: 지난 실행 이후 놓친 메시지를 한 바퀴 훑어서 받는다 ----
    # (예전처럼 파일 하나 받을 때마다 처음부터 다시 훑지 않는다 - 이제는 이 스캔이
    #  끝나면 바로 실시간 리스너로 넘어가서 새 메시지를 즉시 잡아내기 때문에,
    #  한 방향으로 쭉 훑는 것으로 충분하다.)
    print("캐치업 스캔 시작...", flush=True)
    stopped_early = False
    async for message in client.iter_messages(entity):
        checked_count += 1
        if checked_count % 20 == 0:
            print(f"  ...지금까지 {checked_count}개 메시지 확인함 (마지막 확인 날짜: {message.date})", flush=True)

        elapsed = time.monotonic() - start_time
        if elapsed > MAX_RUNTIME_SECONDS:
            print(
                f"실행 시간 제한({MAX_RUNTIME_SECONDS // 60}분) 도달(캐치업 중), "
                "여기서 정상 종료하고 나머지는 다음 실행에서 이어받습니다.",
                flush=True,
            )
            stopped_early = True
            break

        if message.date < CUTOFF:
            print(f"기준일({CUTOFF.date()})보다 오래된 메시지 발견({message.date.date()}), 캐치업 스캔 종료", flush=True)
            break

        await handle_pdf_message(message)

    print(f"캐치업 스캔 완료. 새로 내려받은 PDF: {new_count}개", flush=True)

    # ---- 2) 실시간 리스너: 캐치업이 시간 안에 끝났으면, 새 메시지를 즉시 받는다 ----
    if not stopped_early:
        @client.on(events.NewMessage(chats=entity))
        async def _on_new_message(event):
            try:
                await handle_pdf_message(event.message)
            except Exception as e:
                # 개별 메시지 처리 중 에러가 나도 리스너 자체는 죽지 않게 함.
                print(f"  [새 메시지 처리 중 오류] {e}", flush=True)

        remaining = MAX_RUNTIME_SECONDS - (time.monotonic() - start_time)
        if remaining > 0:
            print(f"실시간 대기 모드 진입 (약 {remaining / 60:.0f}분간 새 메시지를 실시간으로 받습니다)", flush=True)
            try:
                await asyncio.wait_for(client.run_until_disconnected(), timeout=remaining)
            except asyncio.TimeoutError:
                print("실행 시간 제한 도달(대기 중), 정상 종료합니다.", flush=True)

    await client.disconnect()

    # 마지막 파일 처리 중 push가 실패해서 로컬에만 커밋이 남아있을 수 있으니,
    # 컨테이너가 폐기되기 직전에 한 번 더 확실하게 밀어넣는다 (최종 안전장치).
    if AUTO_GIT_PUSH and _has_unpushed_commits():
        print("종료 전 마지막 push 재시도...", flush=True)
        _push_with_retry()

    print(
        f"이번 실행 종료. 새로 내려받은 PDF: {new_count}개 "
        f"(전체 {len(manifest['files'])}개, 용량초과 스킵 {skipped_too_big}개)",
        flush=True,
    )
    return new_count


if __name__ == "__main__":
    result = 0
    try:
        result = asyncio.run(sync())
    finally:
        # sync()가 정상 종료했든 예외로 죽었든, 체인이 끊기지 않도록 항상
        # 다음 실행을 예약한다. (세션 자체가 죽은 경우엔 다음 실행도 금방
        # 똑같이 실패하겠지만, 최소한 사람이 세션을 새로 발급해서 Secret만
        # 갱신하면 그다음 예약된 실행부터 바로 정상화된다.)
        trigger_next_run()

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write(f"new_count={result}\n")
