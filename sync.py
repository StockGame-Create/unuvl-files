"""
텔레그램 방의 PDF 파일을 로컬 폴더(웹사이트 배포 폴더)로 내려받는 스크립트.

동작 방식
---------
- 봇 계정이 아니라 "내 텔레그램 계정"으로 로그인합니다 (Telethon 사용).
  이미 그 방의 멤버로 초대되어 있다면 관리자 권한 없이도 방에 올라온
  모든 메시지/파일을 그대로 읽을 수 있습니다.
- 방의 메시지를 순회하면서 PDF 첨부파일만 찾아 sites/files/ 폴더에 내려받습니다.
- 첨부파일과 함께 올라온 메시지 본문(캡션)에서 "년도 / 강사 / 과목" 같은
  메타데이터와 제목을 최대한 파싱해서 함께 기록합니다. 형식이 없거나
  다른 파일도 있을 수 있으므로, 각 항목은 있으면 채우고 없으면 비워둡니다.
- PDF 첫 페이지를 이미지로 렌더링해서 썸네일로 저장합니다 (sites/thumbnails/).
- 2026-09-04 이후에 올라온 메시지만 대상으로 하며, 그보다 오래된 메시지가
  나오면 그 자리에서 순회를 중단합니다 (메시지는 최신순으로 오므로 효율적).
- 파일 크기가 150MB를 초과하는 PDF는 건너뜁니다.
- sites/manifest.json에 파일 목록(이름, 크기, 날짜, 메타데이터, 썸네일 경로)을
  기록합니다. 웹사이트(index.html)는 이 manifest.json을 읽어서 목록을 보여줍니다.
- 이미 내려받은 파일(manifest에 message_id 존재)은 건너뛰어 중복 다운로드하지 않습니다.
- Firebase, Firestore, 외부 클라우드 API를 전혀 호출하지 않습니다.
  이 스크립트가 하는 일은 "sites/" 폴더를 최신 상태로 만드는 것까지입니다.
  실제 배포(Vercel 등)는 이 저장소에 push되면 자동으로 이루어집니다.

로그인 방식
-----------
- TELEGRAM_SESSION 환경변수(StringSession 문자열)가 있으면 그걸로 로그인합니다.
  (GitHub Actions 등 자동화 환경에서 사용, 인증코드 입력 불필요)
- 없으면 로컬의 telegram_session.session 파일로 로그인합니다.
  (최초 실행 시에만 전화번호/인증코드 입력 필요, 이후엔 파일로 재인증 불필요)
"""

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from telethon import TelegramClient
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

# 한 번 실행에서 최대 이만큼(초)만 다운로드하고 스스로 정상 종료.
# PDF가 아주 많아도 이 시간 안에서 끊고 나가야, 다음 GitHub Actions 스텝(git commit/push)이
# 정상적으로 이어서 실행됨. 남은 파일은 다음 실행(스케줄/수동)에서 이어받음.
MAX_RUNTIME_SECONDS = 20 * 60  # 20분

SITE_DIR = Path("sites")
FILES_DIR = SITE_DIR / "files"
THUMBS_DIR = SITE_DIR / "thumbnails"
MANIFEST_PATH = SITE_DIR / "manifest.json"

FILES_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# GitHub Actions 안에서 실행 중일 때만, 파일 하나 받을 때마다 즉시 git commit + push.
# (로컬에서 그냥 테스트 삼아 돌릴 때는 자동으로 커밋/푸시하지 않도록 방지)
AUTO_GIT_PUSH = os.environ.get("GITHUB_ACTIONS") == "true"

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


def git_commit_and_push(commit_message: str) -> None:
    """sites/ 폴더의 변경사항을 즉시 커밋하고 push. 변경사항 없으면 조용히 넘어감."""
    _run_git("add", "sites/")

    diff = _run_git("diff", "--staged", "--quiet")
    if diff.returncode == 0:
        # 스테이징된 변경사항 없음 (이미 커밋된 상태 등)
        return

    commit = _run_git("commit", "-m", commit_message)
    print(f"    [git commit] {commit.stdout.strip()}{commit.stderr.strip()}", flush=True)

    push = _run_git("push")
    if push.returncode != 0:
        print(f"    [git push 실패] {push.stderr.strip()}", flush=True)
    else:
        print(f"    [git push 성공]", flush=True)


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


async def sync():
    manifest = load_manifest()
    known_message_ids = {f["message_id"] for f in manifest["files"]}

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
    print(f"'{getattr(entity, 'title', CHAT)}' 방에서 PDF를 찾는 중... (기준일: {CUTOFF.date()} 이후, {MAX_SIZE_BYTES // (1024*1024)}MB 이하)", flush=True)

    new_count = 0
    skipped_too_old = 0
    skipped_too_big = 0
    checked_count = 0
    start_time = time.monotonic()
    stopped_early = False

    print("메시지 순회 시작...", flush=True)
    async for message in client.iter_messages(entity):
        checked_count += 1
        if checked_count % 20 == 0:
            print(f"  ...지금까지 {checked_count}개 메시지 확인함 (마지막 확인 날짜: {message.date})", flush=True)

        elapsed = time.monotonic() - start_time
        if elapsed > MAX_RUNTIME_SECONDS:
            print(
                f"실행 시간 제한({MAX_RUNTIME_SECONDS // 60}분) 도달, "
                "여기서 정상 종료하고 나머지는 다음 실행에서 이어받습니다.",
                flush=True,
            )
            stopped_early = True
            break

        if message.date < CUTOFF:
            print(f"기준일({CUTOFF.date()})보다 오래된 메시지 발견({message.date.date()}), 순회 중단", flush=True)
            break

        ok, filename = is_pdf(message)
        if not ok or message.id in known_message_ids:
            continue

        file_size = message.document.size
        if file_size > MAX_SIZE_BYTES:
            print(f"  건너뜀 (용량 초과 {file_size / (1024*1024):.1f}MB): {filename}", flush=True)
            skipped_too_big += 1
            continue

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

        # 캡션(메시지 본문)에서 년도/강사/과목/제목 파싱. 형식이 없거나 달라도
        # 에러 없이 빈 값으로 채워지고, 제목이 없으면 파일명으로 대체.
        meta = parse_caption(message.message)
        title = meta["title"] or Path(filename).stem

        # PDF 첫 페이지 썸네일 생성 (실패해도 목록 자체는 계속 진행)
        thumb_filename = f"{message.id}.png"
        thumb_path = THUMBS_DIR / thumb_filename
        thumbnail_ok = make_thumbnail(local_path, thumb_path)

        manifest["files"].append({
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
        })
        new_count += 1

        # 파일 하나 받을 때마다 바로 manifest를 저장.
        # (끝까지 안 기다리고 timeout 등으로 중간에 멈춰도, 그때까지 받은
        #  파일은 manifest에 확실히 남도록 하기 위함)
        manifest["files"].sort(key=lambda f: f["telegram_date"], reverse=True)
        save_manifest(manifest)

        # 파일 하나 받을 때마다 바로 git commit + push까지 끝냄.
        # (전체 다운로드가 다 끝날 때까지 기다리지 않고, 받는 즉시 웹사이트에 반영되게)
        if AUTO_GIT_PUSH:
            git_commit_and_push(f"chore: PDF 자동 동기화 - {filename} [skip ci]")

    # 혹시 모를 마지막 정렬/저장 (이미 매 다운로드마다 저장되지만 안전하게 한 번 더)
    manifest["files"].sort(key=lambda f: f["telegram_date"], reverse=True)
    save_manifest(manifest)

    print(
        f"완료. 새로 내려받은 PDF: {new_count}개 "
        f"(전체 {len(manifest['files'])}개, 용량초과 스킵 {skipped_too_big}개)"
        + (" [시간 제한으로 중간에 종료, 다음 실행에서 이어받음]" if stopped_early else ""),
        flush=True,
    )
    await client.disconnect()
    return new_count


if __name__ == "__main__":
    result = asyncio.run(sync())
    # GitHub Actions에서 "새 파일이 있었는지"를 다음 스텝(git commit)에 전달하기 위한 출력.
    # 로컬에서 그냥 실행할 때는 무시해도 됩니다.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"new_count={result}\n")