"""
텔레그램 방의 PDF 파일을 로컬 폴더(웹사이트 배포 폴더)로 내려받는 스크립트.

동작 방식
---------
- 봇 계정이 아니라 "내 텔레그램 계정"으로 로그인합니다 (Telethon 사용).
  이미 그 방의 멤버로 초대되어 있다면 관리자 권한 없이도 방에 올라온
  모든 메시지/파일을 그대로 읽을 수 있습니다.
- 방의 메시지를 순회하면서 PDF 첨부파일만 찾아 sites/files/ 폴더에 내려받습니다.
- 2026-09-04 이후에 올라온 메시지만 대상으로 하며, 그보다 오래된 메시지가
  나오면 그 자리에서 순회를 중단합니다 (메시지는 최신순으로 오므로 효율적).
- 파일 크기가 150MB를 초과하는 PDF는 건너뜁니다.
- sites/manifest.json에 파일 목록(이름, 크기, 날짜)을 기록합니다.
  웹사이트(index.html)는 이 manifest.json을 읽어서 목록을 보여줍니다.
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
from datetime import datetime, timezone
from pathlib import Path

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

SESSION_STRING = os.environ.get("TELEGRAM_SESSION")  # GitHub Secrets 등, 없으면 로컬 파일 세션 사용

# ---- 동기화 조건 --------------------------------------------------------
# 이 날짜 이후에 올라온 메시지만 동기화 (하드코딩: 2026-09-04부터)
CUTOFF = datetime(2026, 9, 4, tzinfo=timezone.utc)

# 이 크기(바이트)를 초과하는 PDF는 건너뜀 (150MB)
MAX_SIZE_BYTES = 150 * 1024 * 1024

SITE_DIR = Path("sites")
FILES_DIR = SITE_DIR / "files"
MANIFEST_PATH = SITE_DIR / "manifest.json"

FILES_DIR.mkdir(parents=True, exist_ok=True)


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

    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    else:
        client = TelegramClient("telegram_session", API_ID, API_HASH)  # 로컬 fallback

    await client.start(phone=PHONE)

    entity = await client.get_entity(CHAT)
    print(f"'{getattr(entity, 'title', CHAT)}' 방에서 PDF를 찾는 중... (기준일: {CUTOFF.date()} 이후, {MAX_SIZE_BYTES // (1024*1024)}MB 이하)")

    new_count = 0
    skipped_too_old = 0
    skipped_too_big = 0

    async for message in client.iter_messages(entity):
        if message.date < CUTOFF:
            # 메시지는 최신순으로 오므로, 기준일보다 오래된 게 나오면 더 볼 필요 없음
            break

        ok, filename = is_pdf(message)
        if not ok or message.id in known_message_ids:
            continue

        file_size = message.document.size
        if file_size > MAX_SIZE_BYTES:
            print(f"  건너뜀 (용량 초과 {file_size / (1024*1024):.1f}MB): {filename}")
            skipped_too_big += 1
            continue

        # 파일명이 중복될 수 있으니 메시지 ID를 접두어로 붙여 저장
        safe_filename = f"{message.id}_{filename}"
        local_path = FILES_DIR / safe_filename

        print(f"  내려받는 중: {filename}")
        await client.download_media(message, file=str(local_path))

        manifest["files"].append({
            "message_id": message.id,
            "filename": filename,          # 사람이 보는 원래 파일명
            "stored_as": safe_filename,     # 실제 저장된 파일명 (다운로드 링크에 사용)
            "size_bytes": local_path.stat().st_size,
            "telegram_date": message.date.isoformat(),
        })
        new_count += 1

    # 최신순으로 정렬해서 저장 (텔레그램 날짜 기준)
    manifest["files"].sort(key=lambda f: f["telegram_date"], reverse=True)
    save_manifest(manifest)

    print(
        f"완료. 새로 내려받은 PDF: {new_count}개 "
        f"(전체 {len(manifest['files'])}개, 용량초과 스킵 {skipped_too_big}개)"
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