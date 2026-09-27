// api/view.js
//
// "파일 관리" 탭에서 PDF를 웹사이트 안(<iframe>)에서 바로 볼 수 있게
// 해주는 프록시 엔드포인트.
//
// 왜 이게 필요한가:
// 1) 자료 원본은 GitHub Release 자산(objects.githubusercontent.com)에 있는데,
//    이 URL은 응답에 `Content-Disposition: attachment`가 항상 붙어서 나온다.
//    GitHub이 서명해서 302로 리다이렉트하는 URL 자체의 쿼리스트링에
//    response-content-disposition=attachment 가 박혀있어서, 우리가 요청
//    헤더를 바꾼다고 해도 소용이 없다. 그 결과 <iframe src="다운로드URL">로
//    그대로 열면 "보기"가 아니라 매번 다운로드가 시작돼버린다.
// 2) 브라우저에서 직접 fetch()로 받아와 blob으로 보여주려 해도,
//    objects.githubusercontent.com이 CORS 허용 헤더(Access-Control-Allow-Origin)를
//    안 줘서 막힌다 (ask.js 맨 위 설명에 적힌 것과 정확히 같은 문제).
// 3) 그래서 이 서버 함수가 대신 PDF를 받아와서 Content-Disposition을
//    "inline"으로 바꿔 그대로 응답해준다. 프론트엔드는 이 함수 주소
//    (/api/view?message_id=...)를 <iframe src>로 그대로 쓰면 된다.
//    같은 오리진이라 CORS 문제도 없고, 우리가 응답 헤더를 직접 정하므로
//    Content-Disposition 문제도 해결된다.
//
// 대역폭 관련 주의:
// sync.py / migrate_to_release.py가 애초에 모든 PDF를 GitHub Release로
// 옮긴 이유가 "Vercel Hobby 플랜의 월 100GB 대역폭 한도를 아끼기 위해서"였다.
// 그런데 이 프록시를 거치는 트래픽은 다시 Vercel 함수의 아웃바운드
// 대역폭으로 집계되므로, "보기" 기능은 그 절약 효과를 부분적으로 되돌린다.
// 그래서 index.html의 "다운로드" 버튼(파일 보관함의 작은 버튼, 파일 관리
// 탭의 다운로드 버튼)은 일부러 이 프록시를 거치지 않고 GitHub 쪽 URL로
// 바로 보내도록 되어있다 - "보기"만 이 프록시를 쓰고, "다운로드"는 예전처럼
// 대역폭을 아낀다. 나중에 조회수가 많아져서 부담되면:
//   - Cache-Control(아래 설정됨)로 같은 파일 반복 조회를 브라우저/CDN
//     캐시로 흡수하거나,
//   - Vercel Pro로 올려 대역폭 한도를 늘리거나,
//   - Range 요청(부분 응답)을 지원해서 PDF 뷰어가 필요한 페이지만
//     받아가게 만드는 걸 고려할 것 (지금은 구현 안 함 - 전체를 한 번에
//     내려받아 보여주는 단순한 방식).
//
// 요청 형식: GET /api/view?message_id=123
// 응답: PDF 바이너리 (Content-Type: application/pdf, Content-Disposition: inline)
//       또는 실패 시 JSON { error }

// index.html의 GITHUB_OWNER/GITHUB_REPO/GITHUB_BRANCH와 반드시 동일하게
// 유지해야 한다. (fix_metadata.py가 sync.py의 로직을 그대로 복사해둔 것과
// 같은 이유 - 프론트/백엔드가 서로 다른 파일이라 상수를 공유할 방법이
// 없어서 각자 들고 있는다.)
const GITHUB_OWNER = "StockGame-Create"; // TODO: 실제 GitHub 계정/조직명으로 교체 (index.html과 동일하게)
const GITHUB_REPO = "unvul-files";       // TODO: 실제 저장소 이름으로 교체 (index.html과 동일하게)
const GITHUB_BRANCH = "main";            // index.html과 동일하게 유지
const MANIFEST_URL = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/manifest.json`;
const RAW_FILES_BASE = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/files/`;

// ask.js의 MAX_PDF_BYTES와 같은 이유로 같은 값을 쓴다: 이 함수가 PDF
// 전체를 메모리에 올렸다가(Buffer) 응답하는 단순한 방식이라, 너무 큰
// 파일까지 받으려 하면 Vercel 함수의 메모리/실행시간 한도에 걸릴 수 있다.
// 이 한도를 넘는 파일은 "보기" 대신 다운로드만 안내한다.
// (index.html의 MAX_VIEWABLE_BYTES와 반드시 동일하게 유지 - 프론트에서도
// 같은 기준으로 미리 안내 메시지를 보여준다.)
const MAX_VIEWABLE_BYTES = 100 * 1024 * 1024;

// Vercel Node.js 함수는 보통 req.query를 자동으로 채워주지만, 혹시 비어있는
// 환경/설정에서도 안전하게 동작하도록 req.url에서 직접 한 번 더 파싱해본다.
function getMessageId(req) {
  if (req.query && req.query.message_id !== undefined) {
    return Number(req.query.message_id);
  }
  try {
    const url = new URL(req.url, "http://localhost");
    return Number(url.searchParams.get("message_id"));
  } catch {
    return NaN;
  }
}

module.exports = async function handler(req, res) {
  if (req.method !== "GET") {
    res.status(405).json({ error: "GET만 지원합니다." });
    return;
  }

  const messageId = getMessageId(req);
  if (!Number.isFinite(messageId)) {
    res.status(400).json({ error: "message_id가 올바르지 않습니다." });
    return;
  }

  try {
    const manifestRes = await fetch(MANIFEST_URL, { cache: "no-store" });
    if (!manifestRes.ok) throw new Error("manifest.json을 불러오지 못했습니다.");
    const manifest = await manifestRes.json();
    const file = (manifest.files || []).find((f) => f.message_id === messageId);

    if (!file) {
      res.status(404).json({ error: "해당 자료를 찾을 수 없습니다." });
      return;
    }

    // download_url이 있으면 GitHub Release 자산(대부분의 파일이 여기 해당),
    // 없고 stored_as만 있으면 아직 마이그레이션 전이라 git 저장소에 그대로
    // 있는 옛날 파일 (migrate_to_release.py 참고).
    const pdfUrl = file.download_url
      || (file.stored_as ? `${RAW_FILES_BASE}${encodeURIComponent(file.stored_as)}` : null);

    if (!pdfUrl) {
      res.status(404).json({ error: "원본 파일 위치를 찾을 수 없습니다." });
      return;
    }

    if (file.size_bytes && file.size_bytes > MAX_VIEWABLE_BYTES) {
      res.status(413).json({
        error: `이 자료는 ${(file.size_bytes / (1024 * 1024)).toFixed(0)}MB로 너무 커서 바로 보기는 지원하지 않아요 (현재 한도: ${MAX_VIEWABLE_BYTES / (1024 * 1024)}MB). 다운로드로 받아서 열어주세요.`,
      });
      return;
    }

    const pdfRes = await fetch(pdfUrl);
    if (!pdfRes.ok) throw new Error("원본 PDF를 받아오지 못했습니다.");
    const pdfBuffer = Buffer.from(await pdfRes.arrayBuffer());

    // 파일명에 큰따옴표가 섞여 있으면 헤더 파싱이 깨질 수 있어 제거하고,
    // 한글 등 비-ASCII 파일명도 최대한 살리기 위해 filename*(RFC 5987)도
    // 같이 준다 (filename은 폴백용 ASCII 근사치).
    const rawName = file.filename || "file.pdf";
    const asciiFallbackName = rawName.replace(/["\\]/g, "").replace(/[^\x20-\x7E]/g, "_");

    res.status(200);
    res.setHeader("Content-Type", "application/pdf");
    res.setHeader(
      "Content-Disposition",
      `inline; filename="${asciiFallbackName}"; filename*=UTF-8''${encodeURIComponent(rawName)}`
    );
    res.setHeader("Content-Length", String(pdfBuffer.length));
    // 같은 파일은 자주 안 바뀌니, 잠깐이라도 브라우저/CDN 캐시를 태워서
    // 같은 사람이 반복해서 열어볼 때 이 함수를 다시 거치지 않게 한다.
    res.setHeader("Cache-Control", "public, max-age=3600");
    res.end(pdfBuffer);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message || "알 수 없는 오류가 발생했습니다." });
  }
};

// 대용량 PDF를 통째로 받았다가 내려주려면 기본 10초로는 부족할 수 있어
// 넉넉히 잡는다 (ask.js와 동일한 이유/기본값). Vercel Hobby 플랜 최대 60초,
// Pro면 800초까지 늘릴 수 있다.
module.exports.config = { maxDuration: 60 };
