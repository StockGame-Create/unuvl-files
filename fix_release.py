"""
GitHub Release 자산과 sites/manifest.json 사이의 불일치를 바로잡는 1회성 점검 스크립트.

왜 필요한가
-----------
sync.py는 큰 파일을 GitHub Release 자산으로 올리는데, 예전 버전은 업로드 중
브로큰 파이프/커넥션 끊김이나 422(already_exists)가 나면 곧바로 "실패"로
간주하고 manifest.json에는 아무것도 기록하지 않았다. 하지만 실제로는 서버
쪽 업로드 자체는 끝난 경우가 있었다(응답만 못 받은 것). 그 결과:

  - GitHub Release에는 파일이 실제로 올라가 있는데
  - manifest.json에는 그 텔레그램 메시지가 "받은 적 없음"으로 남아있어서
  - sync.py가 다음 실행 때 그 메시지를 또 새 파일로 보고 다시 내려받아
    다시 올리려다가 already_exists 충돌이 계속 반복된다.

sync.py는 이제 업로드 "시점"에 이런 충돌을 스스로 확인해서 복구하지만, 그건
그 메시지가 다시 스캔 범위에 들어와야 작동한다. 이 스크립트는 기다리지 않고
지금 존재하는 모든 release를 직접 훑어서 manifest에 없는 자산(orphan)을
찾아 처리한다:

  1) 자산 이름(f"{message_id}_{filename}")에서 message_id를 뽑아 manifest에
     이미 있는지 확인한다.
  2) manifest에 없으면 orphan이다. 우선 파일명+용량이 기존 항목과 완전히
     같은지부터 본다 - 같으면 다운로드 없이 "진짜 중복"으로 확정한다.
  3) 애매하면(파일명이 다르거나 처음 보는 파일이면) release에 이미 올라가
     있는 파일을 그대로 내려받아(텔레그램이 아니라 GitHub에서!) 해시를
     계산한다. 기존 파일과 내용이 같으면 중복, 다르면 새 파일로 확정한다.
  4) 새 파일이면 텔레그램에서 그 메시지의 캡션/날짜만 가져와(파일은 이미
     받았으므로 재다운로드하지 않음) manifest에 정식으로 등록한다.
  5) 진짜 중복이면 manifest의 duplicate_message_ids에 추가하고, 여분
     자산은 release에서 지운다 (release당 1000개 한도를 갉아먹지 않도록).

실행 방법
---------
- 기본은 dry-run(미리보기)이다: 무엇을 어떻게 고칠지 로그로만 보여주고
  manifest.json도 GitHub Release도 건드리지 않는다.
- 실제로 적용하려면 환경변수 APPLY_FIXES=true 로 실행한다.
  (fix_release.yml의 workflow_dispatch 입력 "apply" 체크박스가 이 값을 넘긴다.)

주의
----
- sync.py와 똑같은 텔레그램 세션(TELEGRAM_SESSION)을 사용한다. 텔레그램은
  같은 세션에 두 프로세스가 동시에 붙으면 세션 자체를 폐기하므로
  (AuthKeyDuplicatedError), 이 스크립트는 sync.py의 장시간 리스너가 돌고
  있지 않을 때만 실행해야 한다. fix_release.yml에는 sync.yml과 반드시
  같은 concurrency.group을 넣어서 둘이 겹치지 않게 만들어야 한다.
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

import sync  # sync.py의 설정/헬퍼를 그대로 재사용한다. import만 해서는 sync()가 실행되지 않는다.

APPLY_FIXES = os.environ.get("APPLY_FIXES", "").strip().lower() == "true"

TMP_DIR = Path("fix_release_tmp")


def log(msg: str) -> None:
    print(msg, flush=True)


def list_all_release_shards() -> list[tuple[int, str, int]]:
    """존재하는 모든 release 샤드를 (release_id, tag, index) 리스트로 반환한다.
    large-files(1번)부터 시작해서, 태그가 없는 번호가 나오면 거기서 멈춘다
    (sync.py가 항상 순서대로 채워나가므로 중간에 구멍이 나는 일은 없다)."""
    shards = []
    api_base = f"https://api.github.com/repos/{sync.GITHUB_REPOSITORY}/releases"
    for index in range(1, sync.MAX_RELEASE_INDEX + 1):
        tag = sync._tag_for_index(index)
        req = urllib.request.Request(f"{api_base}/tags/{tag}", headers=sync._github_headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                release_id = json.loads(resp.read().decode("utf-8"))["id"]
                shards.append((release_id, tag, index))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            log(f"[release 조회 실패] tag={tag} HTTP {e.code}")
            break
        except Exception as e:
            log(f"[release 조회 실패] tag={tag} {e}")
            break
    return shards


def list_assets(release_id: int) -> list[dict]:
    """release 하나의 모든 자산을 반환한다."""
    items: list[dict] = []
    for page in range(1, 51):
        url = (
            f"https://api.github.com/repos/{sync.GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=sync._github_headers())
        with urllib.request.urlopen(req, timeout=20) as resp:
            batch = json.loads(resp.read().decode("utf-8"))
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def delete_asset(asset_id: int) -> bool:
    url = f"https://api.github.com/repos/{sync.GITHUB_REPOSITORY}/releases/assets/{asset_id}"
    req = urllib.request.Request(url, method="DELETE", headers=sync._github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"    [자산 삭제 실패] id={asset_id} {e}")
        return False


def download_asset_content(url: str, dest: Path) -> None:
    """release 자산을 GitHub에서 직접 내려받는다 (텔레그램이 아니라 GitHub에서 -
    이미 성공적으로 올라가 있는 파일이므로 텔레그램 대역폭/속도 제한을 또
    쓸 필요가 없다)."""
    req = urllib.request.Request(url, headers=sync._github_headers())
    with urllib.request.urlopen(req, timeout=900) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


async def resolve_caption(client, entity, message) -> str | None:
    """sync.py의 resolve_caption과 동일한 로직(앨범 캡션 찾아오기)."""
    if message.message:
        return message.message
    if not message.grouped_id:
        return None
    try:
        ids = list(range(message.id - 9, message.id + 10))
        siblings = await asyncio.wait_for(client.get_messages(entity, ids=ids), timeout=15)
    except Exception as e:
        log(f"    [앨범 캡션 조회 실패] message_id={message.id}: {e}")
        return None
    for sib in siblings:
        if sib and sib.grouped_id == message.grouped_id and sib.message:
            return sib.message
    return None


async def main() -> None:
    if not (sync.GITHUB_REPOSITORY and sync.GH_DISPATCH_TOKEN):
        log("[중단] GITHUB_REPOSITORY 또는 GH_DISPATCH_TOKEN이 없습니다.")
        return

    manifest = sync.load_manifest()
    manifest.setdefault("duplicate_message_ids", [])
    manifest.setdefault("files", [])

    known_message_ids = {f["message_id"] for f in manifest["files"]}
    known_message_ids |= set(manifest["duplicate_message_ids"])
    known_name_size = {(f["filename"].lower(), f["size_bytes"]) for f in manifest["files"]}
    known_hashes = {f["sha256"]: f for f in manifest["files"] if f.get("sha256")}

    log(f"모드: {'적용 (APPLY_FIXES=true)' if APPLY_FIXES else '미리보기 (dry-run, 아무것도 바꾸지 않음)'}")
    log("release 목록 조회 중...")
    shards = list_all_release_shards()
    if not shards:
        log("release를 하나도 찾지 못했습니다. GITHUB_REPOSITORY/GH_DISPATCH_TOKEN을 확인하세요.")
        return
    log(f"release {len(shards)}개 발견: {[tag for _, tag, _ in shards]}")

    orphans = []
    total_assets = 0
    for release_id, tag, _index in shards:
        assets = list_assets(release_id)
        total_assets += len(assets)
        log(f"  {tag}: 자산 {len(assets)}개")
        for asset in assets:
            name = asset["name"]
            if "_" not in name:
                log(f"    [이름 형식 이상] {name} (message_id 접두어 없음, 건너뜀)")
                continue
            prefix, _, filename = name.partition("_")
            if not prefix.isdigit():
                log(f"    [이름 형식 이상] {name} (message_id가 숫자가 아님, 건너뜀)")
                continue
            message_id = int(prefix)
            if message_id in known_message_ids:
                continue
            orphans.append((tag, message_id, filename, asset))

    log(f"\n전체 자산 {total_assets}개 중 manifest에 없는 자산(orphan) {len(orphans)}개 발견")
    if not orphans:
        log("불일치 없음. 고칠 게 없습니다.")
        return

    has_session = bool(sync.SESSION_STRING) or Path("telegram_session.session").exists()
    if not has_session:
        log("[중단] 텔레그램 세션이 없어 캡션/날짜를 가져올 수 없습니다.")
        return

    if sync.SESSION_STRING:
        client = TelegramClient(StringSession(sync.SESSION_STRING), sync.API_ID, sync.API_HASH)
    else:
        client = TelegramClient("telegram_session", sync.API_ID, sync.API_HASH)

    await asyncio.wait_for(client.connect(), timeout=30)
    if not await asyncio.wait_for(client.is_user_authorized(), timeout=30):
        log("[중단] 텔레그램 세션이 유효하지 않습니다.")
        return
    entity = await asyncio.wait_for(client.get_entity(sync.CHAT), timeout=30)

    if APPLY_FIXES and sync.AUTO_GIT_PUSH:
        sync._run_git("config", "user.name", "github-actions[bot]")
        sync._run_git("config", "user.email", "github-actions[bot]@users.noreply.github.com")

    TMP_DIR.mkdir(exist_ok=True)

    fixed = 0
    marked_duplicate = 0
    deleted = 0
    unresolved = 0

    for tag, message_id, filename, asset in orphans:
        size = asset["size"]
        log(f"\n[orphan] message_id={message_id} filename={filename!r} (release={tag}, {size / (1024*1024):.1f}MB)")

        # 1단계: 파일명+용량이 기존 항목과 완전히 같으면, 다운로드 없이 바로
        # "진짜 중복(재업로드)"으로 확정한다 (sync.py의 1단계 검사와 동일한 발상).
        if (filename.lower(), size) in known_name_size:
            log("    파일명+용량이 기존 파일과 일치 -> 다운로드 없이 중복으로 처리")
            marked_duplicate += 1
            if APPLY_FIXES:
                manifest["duplicate_message_ids"].append(message_id)
                known_message_ids.add(message_id)
                if delete_asset(asset["id"]):
                    log("    [여분 자산 삭제 완료]")
                    deleted += 1
                sync.save_manifest(manifest)
            continue

        # 2단계: 애매하면 release에 이미 있는 파일을 내려받아(텔레그램 아님)
        # 해시로 진짜 중복인지, 새 파일인지 확정한다.
        local_path = TMP_DIR / f"{message_id}_{filename}"
        try:
            log("    release에서 자산 내려받는 중 (해시/썸네일 계산용)...")
            download_asset_content(asset["browser_download_url"], local_path)
        except Exception as e:
            log(f"    [자산 다운로드 실패] {e} -> 건너뜀 (다음 실행에서 재시도)")
            unresolved += 1
            continue

        file_hash = sync.compute_sha256(local_path)

        if file_hash in known_hashes:
            original = known_hashes[file_hash]
            log(f"    내용이 기존 '{original['filename']}'와 완전히 같음 -> 진짜 중복으로 처리")
            marked_duplicate += 1
            if APPLY_FIXES:
                manifest["duplicate_message_ids"].append(message_id)
                known_message_ids.add(message_id)
                if delete_asset(asset["id"]):
                    log("    [여분 자산 삭제 완료]")
                    deleted += 1
                sync.save_manifest(manifest)
            local_path.unlink(missing_ok=True)
            continue

        # 3단계: 진짜 새 파일. 텔레그램에서 캡션/날짜만 가져온다 (파일 자체는
        # 이미 받았으니 재다운로드하지 않음).
        try:
            message = await asyncio.wait_for(client.get_messages(entity, ids=message_id), timeout=15)
        except Exception as e:
            log(f"    [텔레그램 메시지 조회 실패] {e} -> 건너뜀 (다음 실행에서 재시도)")
            unresolved += 1
            local_path.unlink(missing_ok=True)
            continue
        if message is None:
            log("    [텔레그램에서 메시지를 찾을 수 없음] (삭제된 메시지일 수 있음) -> 건너뜀")
            unresolved += 1
            local_path.unlink(missing_ok=True)
            continue

        caption_text = await resolve_caption(client, entity, message)
        meta = sync.parse_caption(caption_text)
        title = meta["title"] or Path(filename).stem

        thumb_filename = f"{message_id}.png"
        thumb_path = sync.THUMBS_DIR / thumb_filename
        thumbnail_ok = sync.make_thumbnail(local_path, thumb_path) if APPLY_FIXES else False

        new_entry = {
            "message_id": message_id,
            "filename": filename,
            "stored_as": None,
            "download_url": asset["browser_download_url"],
            "size_bytes": size,
            "telegram_date": message.date.isoformat(),
            "title": title,
            "year": meta["year"],
            "instructor": meta["instructor"],
            "subject": meta["subject"],
            "thumbnail": f"thumbnails/{thumb_filename}" if thumbnail_ok else None,
            "sha256": file_hash,
        }

        log(
            f"    manifest에 새로 등록 -> title={title!r} year={meta['year']} "
            f"instructor={meta['instructor']} subject={meta['subject']}"
        )
        fixed += 1
        if APPLY_FIXES:
            manifest["files"].append(new_entry)
            known_message_ids.add(message_id)
            known_name_size.add((filename.lower(), size))
            known_hashes[file_hash] = new_entry
            manifest["files"].sort(key=lambda f: f["telegram_date"], reverse=True)
            sync.save_manifest(manifest)

        local_path.unlink(missing_ok=True)

    await client.disconnect()

    for p in TMP_DIR.glob("*"):
        p.unlink(missing_ok=True)
    try:
        TMP_DIR.rmdir()
    except OSError:
        pass

    log("\n=== 요약 ===")
    log(f"manifest에 새로 등록{'됨' if APPLY_FIXES else '될 예정'}: {fixed}개")
    log(f"진짜 중복으로 확인{'되어 정리됨' if APPLY_FIXES else '됨 (정리 예정)'}: {marked_duplicate}개 (자산 삭제 {deleted}개)")
    log(f"확인 불가(텔레그램 메시지 없음/다운로드 실패, 다음 실행에서 재시도 필요): {unresolved}개")

    if APPLY_FIXES and sync.AUTO_GIT_PUSH and (fixed or marked_duplicate):
        sync.git_commit_and_push(
            f"chore: fix_release.py로 release/manifest 불일치 {fixed + marked_duplicate}건 정리 [skip ci]"
        )
    elif not APPLY_FIXES:
        log("\n[미리보기 모드] 실제로 적용하려면 APPLY_FIXES=true 로 다시 실행하세요.")


if __name__ == "__main__":
    asyncio.run(main())
