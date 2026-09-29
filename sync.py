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
  메타데이터와 제목을 최대한 파싱해서 함께 기록합니다. 여러 파일을 한 번에
  앨범(그룹)으로 올린 경우 캡션이 그룹 내 메시지 중 하나에만 붙는 경우가
  많은데, 이 경우 같은 그룹의 다른 메시지에서 캡션을 찾아와 공유합니다.
- PDF 첫 페이지를 이미지로 렌더링해서 썸네일로 저장합니다 (sites/thumbnails/).
- 2026-09-04 이후에 올라온 메시지만 대상으로 하며, 그보다 오래된 메시지가
  나오면 캐치업 스캔을 그 자리에서 중단합니다.
- (v5) 파일명을 유니코드 정규화(NFC)해서 다룬다. 한글 등은 완성형(NFC)/조합형
  (NFD) 두 표현이 있어 눈에는 똑같아 보여도 바이트가 달라 '==' 비교가 실패할
  수 있는데, 이 때문에 이미 manifest/release에 있는 파일을 계속 새 파일로
  착각해서 재다운로드/재업로드를 시도하다가 422(already_exists)에 걸리고도
  스스로 복구하지 못하는 문제가 있었다. 파일명을 뽑는 시점(is_pdf)과 이름을
  비교하는 모든 지점(중복 검사, release 자산 조회)에서 정규화를 거친다.
- (v4) GitHub은 release 하나에 자산 1000개까지만 허용하므로, release를 번호로
  나눠 씁니다 (large-files -> large-files-2 -> large-files-3 ...). 업로드 전에
  자산 개수를 세어 990개에 닿으면 다음 release로 넘어가고, 그래도 422
  (file_count)가 나면 재시도 없이 바로 다음 release로 전환합니다. 또한 대용량
  업로드 중 브로큰 파이프/커넥션 끊김이나 422(already_exists)로 실패한 것처럼
  보여도, 실제로는 서버에 업로드가 끝났을 수 있어 재시도 전에 항상 먼저
  확인합니다 - 용량까지 일치하면 재업로드 없이 그 자산을 그대로 쓰고, 이름만
  겹치는 찌꺼기(용량 불일치)면 지우고 다시 올립니다.
- (v3) 파일 크기와 상관없이 모든 PDF를 GitHub Release 자산(asset)으로 업로드
  하고, 그 다운로드 URL만 manifest.json에 기록합니다. git commit으로는 더
  이상 아무 PDF도 저장하지 않습니다. 1.9GB를 초과하면 건너뜁니다.
  (예전에는 95MB 이하만 git commit으로 저장했는데, Vercel 같은 정적 호스팅이
   sites/files/를 그대로 서빙하면서 대역폭 한도를 순식간에 다 먹어버리는
   문제가 있어서, PDF 다운로드 트래픽을 전부 GitHub 쪽으로 옮기기로 했다.)
- sites/manifest.json에 파일 목록(이름, 크기, 날짜, 메타데이터, 썸네일 경로)을
  기록합니다. 웹사이트(index.html)는 이 manifest.json을 읽어서 목록을 보여줍니다.
  (vercel.json으로 Vercel 자동 배포를 꺼뒀기 때문에, index.html은 이 파일을
   Vercel 배포 결과물이 아니라 raw.githubusercontent.com에서 직접 fetch한다.
   즉 git push만 되면 되고, 배포가 몇 번 일어나는지는 더 이상 신경 쓸 필요 없다.)
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
import unicodedata
import urllib.error
import urllib.parse
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
CUTOFF = datetime(2025, 2, 4, tzinfo=timezone.utc)

# ---- 저장 방식 ----------------------------------------------------------
# (v3) 크기 상관없이 전부 GitHub Release 자산(asset)으로 업로드한다.
# Release asset은 git 저장소 100MB 제한과 무관하게 파일당 2GB까지 허용되고,
# 별도 과금되는 대역폭 쿼터도 없다. manifest.json에는 로컬 경로 대신
# 다운로드 URL만 기록한다 (index.html이 download_url 유무로 분기해서
# 다운로드 링크를 만듦).
RELEASE_TAG = "large-files"              # release 태그의 기본 이름. 1번은 이 이름 그대로, 2번부터 -2, -3 ... 자동 생성
RELEASE_ASSET_LIMIT = 1000               # GitHub이 release 하나에 허용하는 자산 개수 상한 (참고용)
RELEASE_SOFT_LIMIT = 990                 # 이 개수에 닿으면 다음 release로 넘어감 (상한에 딱 맞추면 아슬아슬해서 여유를 둠)
MAX_RELEASE_INDEX = 200                  # release 번호 상한 (무한 루프 방지용 안전장치)
MAX_ROTATIONS_PER_UPLOAD = 5             # 파일 하나 올리다가 release를 연달아 바꿀 수 있는 최대 횟수
MAX_SIZE_BYTES = 1900 * 1024 * 1024      # GitHub release asset 한도(2GB)에 여유를 둔 최종 상한

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
# 파일마다 push해도 Vercel 배포가 매번 새로 생기지 않도록 vercel.json에서
# git.deploymentEnabled를 껐으므로, 배치로 묶을 필요 없이 즉시 push하는 게 가장 단순하다.
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


def normalize_name(name: str | None) -> str | None:
    """유니코드 정규화(NFC)를 적용한다. 한글 등은 완성형(NFC)/조합형(NFD) 두 표현이
    있는데 눈에는 똑같아 보여도 바이트로는 다른 문자열이라 '==' 비교가 실패한다.
    파일명이 오간 경로(텔레그램 API, GitHub API, 로컬 파일시스템, 과거 실행 결과)가
    저마다 다른 정규화 형태를 쓸 수 있어서, 이름을 비교하거나 저장하기 전에는
    항상 이걸 거쳐서 형태를 통일한다."""
    return unicodedata.normalize("NFC", name) if name is not None else None


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
        # 여기서 바로 정규화해서, 이후 모든 곳(로컬 파일명, release 자산 이름,
        # manifest 저장, 중복 검사)이 하나의 통일된 형태만 다루게 만든다.
        return True, normalize_name(filename) or f"document_{message.id}.pdf"
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


# ---- GitHub Release 자산 업로드 (샤딩: 1000개 제한 대응) ---------------------
# GitHub은 release 하나당 자산을 최대 1000개까지만 허용한다(초과 시 HTTP 422
# "file_count limited to 1000 assets per release"). 그래서 release를 번호로
# 나눠서 쓴다: large-files(1번) -> large-files-2 -> large-files-3 ...
# 이미 올라간 파일은 manifest의 download_url에 태그가 박혀 있으므로 영향 없다.
_release_state: dict = {"index": 1, "id": None, "count": None}


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _tag_for_index(index: int) -> str:
    """1번은 기존 태그(large-files) 그대로, 2번부터는 large-files-2, -3 ..."""
    return RELEASE_TAG if index == 1 else f"{RELEASE_TAG}-{index}"


def _count_release_assets(release_id: int) -> int | None:
    """release에 실제로 올라가 있는 자산 개수를 API로 센다 (페이지네이션 처리)."""
    total = 0
    for page in range(1, 51):  # 최대 5000개까지 (사실상 무제한)
        url = (
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=_github_headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"    [release 자산 개수 조회 실패] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
            return None
        except Exception as e:
            print(f"    [release 자산 개수 조회 실패] {e}", flush=True)
            return None
        total += len(items)
        if len(items) < 100:
            break
    return total


def _load_release(index: int) -> tuple[int, int] | None:
    """index번 release를 찾아 (id, 현재 자산 개수)를 반환. 없으면 새로 만든다."""
    tag = _tag_for_index(index)
    api_base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"

    release_id = None
    req = urllib.request.Request(f"{api_base}/tags/{tag}", headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_id = json.loads(resp.read().decode("utf-8"))["id"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"    [release 조회 실패] tag={tag} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
            return None
    except Exception as e:
        print(f"    [release 조회 실패] tag={tag} {e}", flush=True)
        return None

    if release_id is not None:
        count = _count_release_assets(release_id)
        if count is None:
            return None
        print(f"    [release 사용] tag={tag} (현재 자산 {count}개)", flush=True)
        return release_id, count

    body = json.dumps({
        "tag_name": tag,
        "name": f"PDF 파일 저장소 #{index}",
        "body": (
            "sync.py가 PDF를 모아두는 release입니다. GitHub의 release당 자산 1000개 제한 때문에 "
            "번호를 붙여 여러 개로 나눠 씁니다. 자동으로 관리되니 직접 수정하지 마세요."
        ),
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base, data=body, method="POST",
        headers={**_github_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_id = json.loads(resp.read().decode("utf-8"))["id"]
            print(f"    [release 생성됨] tag={tag}", flush=True)
            return release_id, 0
    except urllib.error.HTTPError as e:
        print(f"    [release 생성 실패] tag={tag} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
        return None
    except Exception as e:
        print(f"    [release 생성 실패] tag={tag} {e}", flush=True)
        return None


def _find_asset(release_id: int, asset_name: str) -> dict | None:
    """release 안에서 이름이 asset_name인 자산을 찾아 {id, size, browser_download_url}을
    반환한다. 없으면 None. (already_exists 충돌이나, 업로드는 성공했는데 응답을
    못 받아 실패로 오인한 경우를 확인하는 데 쓴다.)"""
    for page in range(1, 51):
        url = (
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=_github_headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"    [자산 조회 실패] {e}", flush=True)
            return None
        for item in items:
            # 정규화해서 비교: GitHub에 저장된 이름이 (예전 코드가 만들었거나,
            # 다른 경로로 올라와서) 다른 유니코드 정규화 형태일 수 있어서,
            # 바이트 그대로 비교하면 눈에는 같은 이름인데도 못 찾는 경우가 있다.
            if normalize_name(item.get("name")) == normalize_name(asset_name):
                return {
                    "id": item["id"],
                    "size": item.get("size"),
                    "browser_download_url": item["browser_download_url"],
                }
        if len(items) < 100:
            break
    return None


def _delete_asset(asset_id: int) -> bool:
    """찌꺼기/손상된 자산을 지운다 (이름 충돌을 풀고 재업로드하기 위함)."""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/assets/{asset_id}"
    req = urllib.request.Request(url, method="DELETE", headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        print(f"    [자산 삭제 실패] id={asset_id} {e}", flush=True)
        return False


def _reconcile_existing_asset(release_id: int, asset_name: str, expected_size: int) -> str | None:
    """업로드가 실패한 것처럼 보였을 때, 사실은 서버에 이미 올라가 있는지 확인한다.
    용량까지 일치하면 그 자산을 그대로 쓰고(재업로드 안 함) URL을 반환한다.
    이름은 같은데 용량이 다르면(부분 업로드 등 찌꺼기) 지우고 None을 반환해서
    호출부가 새로 업로드하게 한다. 아예 없으면 None."""
    existing = _find_asset(release_id, asset_name)
    if existing is None:
        return None
    if existing["size"] == expected_size:
        print(
            f"    [업로드 확인됨] {asset_name} 는 이미 release에 정상적으로 올라가 있음 "
            "(이전 시도가 응답만 못 받고 실제로는 성공했던 것으로 보임)",
            flush=True,
        )
        return existing["browser_download_url"]
    print(
        f"    [찌꺼기 자산 발견] {asset_name} (용량 불일치: 서버 {existing['size']} vs "
        f"로컬 {expected_size}) -> 삭제 후 재업로드",
        flush=True,
    )
    _delete_asset(existing["id"])
    return None


def _advance_release() -> bool:
    """현재 release를 '가득 참'으로 보고 다음 번호로 넘어간다. 한도(MAX_RELEASE_INDEX)를 넘으면 False."""
    if _release_state["index"] >= MAX_RELEASE_INDEX:
        return False
    _release_state["index"] += 1
    _release_state["id"] = None
    _release_state["count"] = None
    return True


def _current_release_id() -> int | None:
    """지금 업로드에 쓸 release id를 반환. 로드/생성이 필요하면 하고,
    자산 개수가 RELEASE_SOFT_LIMIT 이상이면 자동으로 다음 번호로 넘어간다."""
    while True:
        if _release_state["id"] is None:
            loaded = _load_release(_release_state["index"])
            if loaded is None:
                return None
            _release_state["id"], _release_state["count"] = loaded

        if _release_state["count"] >= RELEASE_SOFT_LIMIT:
            print(
                f"    [release 가득 참] tag={_tag_for_index(_release_state['index'])} "
                f"({_release_state['count']}개) -> 다음 release로 넘어갑니다",
                flush=True,
            )
            if not _advance_release():
                print(f"    [release 번호 한도({MAX_RELEASE_INDEX}) 초과]", flush=True)
                return None
            continue

        return _release_state["id"]


def upload_release_asset(local_path: Path, asset_name: str, max_retries: int = 5) -> str | None:
    """PDF를 GitHub Release 자산으로 업로드하고 다운로드 URL을 반환한다.

    현재 release가 꽉 찼으면(미리 센 개수 또는 422 file_count 응답) 재시도 횟수를
    쓰지 않고 바로 다음 번호의 release로 바꿔서 다시 올린다.

    업로드가 실패한 것처럼 보이는 경우(422 already_exists, 또는 브로큰 파이프/커넥션
    끊김 같은 네트워크 에러) 대용량 파일은 실제로는 서버에 업로드가 끝났는데 그
    응답만 못 받아서 실패로 오인하는 경우가 흔하다. 그래서 어떤 이유로 실패하든,
    재시도하기 전에 먼저 release에 같은 이름의 자산이 이미 올라가 있고 용량까지
    일치하는지 확인한다 - 맞으면 재업로드 없이 그 URL을 그대로 쓴다.

    끝내 실패하면 None을 반환하며, 호출부는 이 메시지를 known으로 기록하지 않고
    넘어가서 다음 실행에서 자연스럽게 재시도하게 된다."""
    if not (GITHUB_REPOSITORY and GH_DISPATCH_TOKEN):
        print("    [release 업로드 건너뜀] GITHUB_REPOSITORY 또는 GH_DISPATCH_TOKEN이 없음", flush=True)
        return None

    file_size = local_path.stat().st_size
    attempt = 0
    rotations = 0
    reconcile_tries = 0  # already_exists 확인이 실패해서 재확인한 횟수 (무한루프 방지용 상한)
    MAX_RECONCILE_TRIES = 5

    while attempt < max_retries:
        release_id = _current_release_id()
        if release_id is None:
            return None

        upload_url = (
            f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?name={urllib.parse.quote(asset_name)}"
        )

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
                        "Connection": "close",
                    },
                )
                with urllib.request.urlopen(req, timeout=900) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    _release_state["count"] = (_release_state["count"] or 0) + 1
                    print(
                        f"    [release 업로드 성공] {asset_name} "
                        f"(tag={_tag_for_index(_release_state['index'])}, {_release_state['count']}개째)",
                        flush=True,
                    )
                    return data["browser_download_url"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            if e.code == 422 and "file_count" in detail:
                # 개수를 잘못 셌거나 다른 곳에서 채워진 경우: 시도 횟수/대기 없이 바로 다음 release로.
                rotations += 1
                print(
                    f"    [release 자산 한도 도달] tag={_tag_for_index(_release_state['index'])} "
                    f"-> 다음 release로 전환 ({rotations}회째)",
                    flush=True,
                )
                if rotations > MAX_ROTATIONS_PER_UPLOAD or not _advance_release():
                    print(f"    [release 전환 한도 초과] {asset_name}", flush=True)
                    return None
                continue
            if e.code == 422 and "already_exists" in detail:
                print(
                    f"    [release 업로드 실패 (시도 {attempt + 1}/{max_retries})] "
                    f"HTTP {e.code} already_exists -> 실제로 이미 올라가 있는지 확인", flush=True,
                )
                reconciled = _reconcile_existing_asset(release_id, asset_name, file_size)
                if reconciled is not None:
                    _release_state["count"] = (_release_state["count"] or 0) + 1
                    return reconciled
                # 자산을 못 찾았거나(막 생성된 자산이 목록 API에 아직 안 뜬 경우) 찌꺼기라서
                # 지웠거나 - 어느 쪽이든 이 경로는 무한 루프로 새지 않도록 반드시 횟수와
                # 대기시간을 둔다. (예전 버그: 여기서 카운트/대기 없이 곧바로 continue 해서,
                # 자산 목록 반영이 늦어지면 이 파일 하나에 영원히 멈춰 뒤의 새 파일들이
                # 아예 처리되지 못했음 - 동기 코드라 이벤트 루프 전체가 막힘.)
                reconcile_tries += 1
                attempt += 1
                if reconcile_tries >= MAX_RECONCILE_TRIES:
                    print(f"    [already_exists 재확인 한도 초과] {asset_name} -> 이번 실행은 포기", flush=True)
                    break
                time.sleep(min(2 * reconcile_tries, 10))
                continue
            attempt += 1
            print(f"    [release 업로드 실패 (시도 {attempt}/{max_retries})] HTTP {e.code}: {detail}", flush=True)
        except Exception as e:
            # 브로큰 파이프/커넥션 리셋 등: 업로드 자체는 서버에 끝났는데 응답만
            # 못 받았을 가능성이 있으므로, 실패로 단정하기 전에 먼저 확인해본다.
            print(
                f"    [release 업로드 중 네트워크 오류 (시도 {attempt + 1}/{max_retries})] {e} "
                "-> 실제로 업로드가 됐는지 확인", flush=True,
            )
            reconciled = _reconcile_existing_asset(release_id, asset_name, file_size)
            if reconciled is not None:
                _release_state["count"] = (_release_state["count"] or 0) + 1
                return reconciled
            attempt += 1
            print(f"    [release 업로드 실패 (시도 {attempt}/{max_retries})] {e}", flush=True)
        time.sleep(min(2 ** attempt, 30))

    print(f"    [release 업로드 최종 실패] {asset_name}", flush=True)
    return None


async def sync():
    manifest = load_manifest()
    manifest.setdefault("duplicate_message_ids", [])

    known_message_ids = {f["message_id"] for f in manifest["files"]}
    known_message_ids |= set(manifest["duplicate_message_ids"])

    # 1단계(다운로드 전) 중복 검사용: 파일명+용량이 완전히 같으면 십중팔구 재업로드.
    # 파일명은 유니코드 정규화(NFC) 후 비교한다 - 과거 항목이 다른 정규화
    # 형태로 저장돼 있으면 눈에는 같은 이름인데도 매칭이 안 되는 문제가 있었다.
    known_name_size = {(normalize_name(f["filename"]).lower(), f["size_bytes"]) for f in manifest["files"]}
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
    print(
        f"'{getattr(entity, 'title', CHAT)}' 방 감시 시작 (기준일: {CUTOFF.date()} 이후, "
        f"모든 PDF는 GitHub Release로 저장, {MAX_SIZE_BYTES // (1024*1024)}MB 초과 시 건너뜀)",
        flush=True,
    )

    new_count = 0
    skipped_too_big = 0
    checked_count = 0
    start_time = time.monotonic()

    async def resolve_caption(message) -> str | None:
        """메시지 자신의 캡션을 우선 사용하되, 없고 이 메시지가 앨범(그룹 전송)의
        일부라면 같은 grouped_id를 가진 다른 메시지에서 캡션을 찾아온다.
        텔레그램 앨범은 캡션이 그룹 내 메시지 중 하나에만 붙는 경우가 흔해서,
        (예: PDF 2개를 캡션 하나로 같이 올린 경우) 이걸 안 하면 캡션이 없는
        메시지 쪽은 년도/강사/과목이 전부 비어버린다.
        앨범 아이템은 message id가 항상 연속이므로 주변 id를 조회해서 찾는다."""
        if message.message:
            return message.message
        if not message.grouped_id:
            return None
        try:
            ids = list(range(message.id - 9, message.id + 10))
            siblings = await asyncio.wait_for(
                client.get_messages(entity, ids=ids), timeout=15
            )
        except Exception as e:
            print(f"    [앨범 캡션 조회 실패] message_id={message.id}: {e}", flush=True)
            return None
        for sib in siblings:
            if sib and sib.grouped_id == message.grouped_id and sib.message:
                return sib.message
        return None

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
        actual_size = local_path.stat().st_size

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
        # (앨범으로 묶여 전송된 경우, 이 메시지 자체엔 캡션이 없을 수 있어
        #  resolve_caption이 같은 그룹의 다른 메시지에서 캡션을 찾아온다.)
        caption_text = await resolve_caption(message)
        meta = parse_caption(caption_text)
        title = meta["title"] or Path(filename).stem

        # PDF 첫 페이지 썸네일 생성 (실패해도 목록 자체는 계속 진행)
        thumb_filename = f"{message.id}.png"
        thumb_path = THUMBS_DIR / thumb_filename
        thumbnail_ok = make_thumbnail(local_path, thumb_path)

        # (v3) 크기 상관없이 전부 Release로 업로드한다. git에는 아예 올리지 않는다.
        print(f"    (release로 업로드 중): {filename}", flush=True)
        download_url = upload_release_asset(local_path, safe_filename)
        if download_url is None:
            print(f"  건너뜀 (release 업로드 실패, 다음 실행에서 재시도): {filename}", flush=True)
            local_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
            return False
        # git에는 올리지 않으므로 로컬에서 지운다 (git add 시 실수로 커밋되는 것 방지).
        local_path.unlink(missing_ok=True)
        stored_as = None

        new_entry = {
            "message_id": message.id,
            "filename": filename,          # 사람이 보는 원래 파일명
            "stored_as": stored_as,         # git으로 저장된 경우의 파일명 (release인 경우 None)
            "download_url": download_url,   # release로 저장된 경우의 다운로드 URL (git인 경우 None)
            "size_bytes": actual_size,
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
        known_name_size.add((filename.lower(), actual_size))
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
