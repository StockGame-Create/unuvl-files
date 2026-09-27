# fix_metadata.py
# 일회성 백필 스크립트: manifest.json에서 year/instructor/subject 중
# 하나라도 null인 항목을 다시 검사해서, "앨범(그룹 전송)이라 캡션이
# 이 메시지에는 안 붙어서" 비어있던 경우만 채워준다.
#
# 안전장치:
# - 이미 값이 있는 필드는 절대 덮어쓰지 않는다.
# - 텔레그램을 다시 조회해도 그 그룹 어디에도 정보가 없으면
#   (=진짜로 캡션에 년도/강사/과목이 없던 파일) 그대로 null로 남겨둔다.
#   이건 버그가 아니라 원래 그런 파일이므로 절대 손대지 않는다.
# - title은 "파일명에서 따온 기본값"인지 "실제 캡션 제목"인지 구분할
#   방법이 없어서 이 스크립트에서는 건드리지 않는다. (필요하면 별도 요청)
#
# 실행 후 git add/commit/push는 직접 해주면 된다 (자동 커밋 안 함 -
# 결과를 먼저 눈으로 확인하고 커밋하는 걸 권장).

import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

_chat_raw = os.environ["TELEGRAM_CHAT"]
CHAT = int(_chat_raw) if _chat_raw.lstrip("-").isdigit() else _chat_raw

SESSION_STRING = os.environ.get("TELEGRAM_SESSION", "").strip() or None

MANIFEST_PATH = Path("sites/manifest.json")

# sync.py의 parse_caption과 동일한 로직 (독립 실행 스크립트라 그대로 복사)
_LABEL_PATTERNS = {
    "year": re.compile(r"^[^\w가-힣]*년도\s*[:：]\s*(.+)$"),
    "instructor": re.compile(r"^[^\w가-힣]*강사\s*[:：]\s*(.+)$"),
    "subject": re.compile(r"^[^\w가-힣]*과목\s*[:：]\s*(.+)$"),
}


def parse_caption(caption: str | None) -> dict:
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
            continue
        if result["title"] is None:
            result["title"] = line
    return result


async def resolve_caption(client, entity, grouped_id, own_caption, msg_id) -> str | None:
    """자기 캡션이 있으면 그걸 쓰고, 없고 앨범(grouped_id)의 일부라면
    같은 그룹의 다른 메시지에서 캡션을 찾아온다. 그래도 없으면 None."""
    if own_caption:
        return own_caption
    if not grouped_id:
        return None
    try:
        ids = list(range(msg_id - 9, msg_id + 10))
        siblings = await asyncio.wait_for(client.get_messages(entity, ids=ids), timeout=15)
    except Exception as e:
        print(f"  [조회 실패] id={msg_id}: {e}", flush=True)
        return None
    for sib in siblings:
        if sib and sib.grouped_id == grouped_id and sib.message:
            return sib.message
    return None


async def main():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    targets = [
        f for f in manifest["files"]
        if f.get("year") is None or f.get("instructor") is None or f.get("subject") is None
    ]
    print(f"검사 대상: {len(targets)}개 (전체 {len(manifest['files'])}개 중 하나라도 null인 항목)", flush=True)

    if not targets:
        print("검사할 항목이 없습니다.", flush=True)
        return

    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    else:
        client = TelegramClient("telegram_session", API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "세션이 유효하지 않습니다. generate_session.py로 새로 발급받아 "
            "TELEGRAM_SESSION 값을 갱신하세요."
        )

    entity = await client.get_entity(CHAT)

    updated_count = 0
    untouched_no_info = 0

    for f in targets:
        msg_id = f["message_id"]
        try:
            msg = await client.get_messages(entity, ids=msg_id)
        except Exception as e:
            print(f"  [메시지 조회 실패] {f['filename']} (id={msg_id}): {e}", flush=True)
            continue

        if msg is None:
            print(f"  [메시지를 찾을 수 없음(삭제됨?)] {f['filename']} (id={msg_id})", flush=True)
            continue

        caption = await resolve_caption(client, entity, msg.grouped_id, msg.message, msg_id)
        if not caption:
            # 진짜로 캡션 정보가 없는 파일 - 버그가 아니므로 그대로 둔다.
            untouched_no_info += 1
            continue

        meta = parse_caption(caption)

        changed_fields = []
        for key in ("year", "instructor", "subject"):
            if f.get(key) is None and meta.get(key) is not None:
                f[key] = meta[key]
                changed_fields.append(key)

        if changed_fields:
            updated_count += 1
            print(f"  [수정] {f['filename']}: {', '.join(changed_fields)} 채움", flush=True)

    await client.disconnect()

    print(
        f"\n결과: {updated_count}개 수정됨 / "
        f"{untouched_no_info}개는 정말 캡션 정보가 없어서 그대로 둠 / "
        f"검사대상 {len(targets)}개",
        flush=True,
    )

    if updated_count:
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("manifest.json 저장 완료. 확인 후 git add/commit/push 해주세요.", flush=True)
    else:
        print("변경된 내용이 없어 manifest.json을 다시 저장하지 않았습니다.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
