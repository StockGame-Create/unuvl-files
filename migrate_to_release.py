# migrate_to_release.py
# 일회성 마이그레이션 스크립트.
#
# 지금까지 git commit으로 sites/files/에 저장돼있던 PDF들을 전부 GitHub
# Release 자산으로 옮기고, manifest.json의 해당 항목을 download_url 방식으로
# 바꾼다. 옮긴 뒤에는 로컬 파일을 삭제해서(=git에서 빠지도록) 다음 커밋에
# 반영되게 한다.
#
# 배경: Vercel 같은 정적 호스팅이 sites/files/의 PDF를 그대로 서빙하면서
# 대역폭 한도(Hobby 플랜 월 100GB)를 순식간에 다 먹어버려서 배포가
# 정지되는 문제가 있었다. PDF 다운로드 트래픽을 전부 GitHub Release 쪽으로
# 옮기면 이 문제가 사라진다. sync.py는 이미 이 방식으로 바뀌었고, 이
# 스크립트는 "이미 git에 커밋된 과거 파일들"만 따로 한 번 옮겨주는 용도다.
#
# 텔레그램 접속이 필요 없어서 TELEGRAM_* 환경변수는 전혀 안 쓴다.
# 필요한 건 GITHUB_REPOSITORY와 GH_DISPATCH_TOKEN(release 생성/업로드 권한)뿐.
#
# 실행 중간에 죽어도 안전하다: 파일 하나 옮길 때마다 바로 manifest.json을
# 저장하므로, 다시 실행하면 이미 옮긴 건 건너뛰고 나머지만 이어서 처리한다.

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_DIR = Path("sites")
FILES_DIR = SITE_DIR / "files"
MANIFEST_PATH = SITE_DIR / "manifest.json"

GITHUB_REPOSITORY = os.environ["GITHUB_REPOSITORY"]  # "owner/repo" (Actions에서는 자동 설정됨)
GH_TOKEN = os.environ["GH_DISPATCH_TOKEN"]  # contents:write 권한 필요 (release 생성/업로드용)
RELEASE_TAG = "large-files"  # sync.py와 반드시 같은 태그를 써야 한 release에 모인다


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


_release_cache: dict = {"id": None}


def _get_or_create_release_id() -> int | None:
    """sync.py와 같은 RELEASE_TAG의 release id를 찾아서 반환. 없으면 새로 만든다."""
    if _release_cache["id"] is not None:
        return _release_cache["id"]

    api_base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"

    req = urllib.request.Request(f"{api_base}/tags/{RELEASE_TAG}", headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _release_cache["id"] = data["id"]
            return data["id"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  [release 조회 실패] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
            return None
    except Exception as e:
        print(f"  [release 조회 실패] {e}", flush=True)
        return None

    body = json.dumps({
        "tag_name": RELEASE_TAG,
        "name": "대용량 파일 저장소 (git 100MB 제한 초과분)",
        "body": "sync.py가 자동으로 관리하는 release입니다. 직접 수정하지 마세요.",
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base, data=body, method="POST",
        headers={**_github_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _release_cache["id"] = data["id"]
            print(f"  [release 생성됨] tag={RELEASE_TAG}", flush=True)
            return data["id"]
    except Exception as e:
        print(f"  [release 생성 실패] {e}", flush=True)
        return None


def upload_release_asset(local_path: Path, asset_name: str, max_retries: int = 3) -> str | None:
    """파일을 GitHub Release 자산으로 업로드하고 다운로드 URL을 반환. 실패하면 None."""
    release_id = _get_or_create_release_id()
    if release_id is None:
        return None

    upload_url = (
        f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/"
        f"{release_id}/assets?name={urllib.parse.quote(asset_name)}"
    )
    file_size = local_path.stat().st_size

    for attempt in range(1, max_retries + 1):
        try:
            with open(local_path, "rb") as f:
                req = urllib.request.Request(
                    upload_url,
                    data=f,
                    method="POST",
                    headers={
                        **_github_headers(),
                        "Content-Type": "application/pdf",
                        "Content-Length": str(file_size),
                    },
                )
                with urllib.request.urlopen(req, timeout=900) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    print(f"  [업로드 성공] {asset_name}", flush=True)
                    return data["browser_download_url"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            print(f"  [업로드 실패 (시도 {attempt}/{max_retries})] HTTP {e.code}: {detail}", flush=True)
        except Exception as e:
            print(f"  [업로드 실패 (시도 {attempt}/{max_retries})] {e}", flush=True)
        time.sleep(min(2 ** attempt, 20))

    print(f"  [업로드 최종 실패] {asset_name}", flush=True)
    return None


def main():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    # git에 저장돼있고(stored_as 존재) 아직 release로 안 옮겨진(download_url 없음) 것만 대상.
    targets = [
        f for f in manifest["files"]
        if f.get("stored_as") and not f.get("download_url")
    ]
    print(f"마이그레이션 대상: {len(targets)}개 (전체 {len(manifest['files'])}개 중)", flush=True)

    if not targets:
        print("옮길 파일이 없습니다.", flush=True)
        return

    migrated = 0
    failed = 0

    for f in targets:
        local_path = FILES_DIR / f["stored_as"]
        if not local_path.exists():
            print(f"  [로컬 파일 없음, 건너뜀] {f['filename']} ({local_path})", flush=True)
            failed += 1
            continue

        print(f"업로드 중: {f['filename']} ({f['size_bytes'] / (1024*1024):.1f}MB)", flush=True)
        download_url = upload_release_asset(local_path, f["stored_as"])
        if download_url is None:
            failed += 1
            continue

        f["download_url"] = download_url
        f["stored_as"] = None
        local_path.unlink(missing_ok=True)  # 다음 git commit에서 자동으로 삭제 반영되도록
        migrated += 1

        # 하나씩 성공할 때마다 바로 저장 (중간에 죽어도 진행 상황 보존)
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n완료: {migrated}개 이전 성공 / {failed}개 실패(다음 실행에서 재시도 가능)", flush=True)


if __name__ == "__main__":
    main()
