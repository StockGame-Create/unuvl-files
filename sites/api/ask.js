// api/ask.js
//
// "파일 선택 후 질문하기" AI 기능의 백엔드.
//
// 동작 방식 (미리 렌더링 안 함, 질문할 때만 그 파일 하나를 처리):
// 1) manifest.json에서 선택된 파일의 원본 PDF 위치(GitHub Release 다운로드 URL)를 찾는다.
// 2) 서버(이 함수) 쪽에서 그 PDF를 받아온다. 브라우저가 아니라 서버가 받는 거라
//    CORS 문제가 없다 (브라우저 fetch만 objects.githubusercontent.com의 CORS에 막힌다).
// 3) 받은 PDF를 Gemini Files API에 그대로 업로드한다. Gemini는 PDF를 내부적으로
//    페이지 이미지로 변환해서 이해하기 때문에, 우리가 따로 페이지를 이미지로
//    렌더링해둘 필요가 없다 (스캔 이미지 위주인 자료에도 그대로 통함).
// 4) 업로드된 파일 URI + 질문을 generateContent에 같이 넘겨서 답변을 받는다.
//
// 이 방식의 트레이드오프: sync.py에서 미리 처리해두는 게 없는 대신, 질문할 때마다
// 그 파일의 원본을 통째로 내려받고 Gemini에 다시 업로드하는 과정을 거치므로
// 첫 질문 응답이 몇 초~몇십 초 걸릴 수 있다 (파일이 클수록 오래 걸림). 그래서
// 아래 MAX_PDF_BYTES로 너무 큰 파일은 아예 걸러서 타임아웃 대신 명확한 안내를 준다.
//
// 필요한 환경변수 (Vercel 프로젝트 설정 > Environment Variables에 추가해야 함.
// GitHub Secrets의 GEMINI_API_KEY와는 별개의 저장소이니 반드시 여기에도 등록할 것):
//   GEMINI_API_KEY
//
// 요청 형식: POST { message_id: number, question: string }
// 응답 형식: { answer: string } 또는 { error: string }

const GEMINI_MODEL = "gemini-3-flash"; // 2026년 기준 무료 티어 기본 모델, PDF 네이티브 이해 지원
const MAX_QUESTION_LENGTH = 1000;

// Vercel 서버리스 함수 실행시간 한도(아래 config.maxDuration) 안에 "다운로드 +
// Gemini 업로드 + 답변 생성"이 다 끝나야 하므로, 너무 큰 파일은 아예 거절한다.
// Hobby(무료) 플랜은 함수 실행시간이 최대 60초라 넉넉하게 잡기 어렵다 - 일단
// 100MB로 시작하고, 실제로 타임아웃이 잦으면 더 낮추면 된다. Vercel Pro면
// maxDuration을 최대 800초까지 늘릴 수 있어서 이 값도 같이 올릴 수 있다.
const MAX_PDF_BYTES = 100 * 1024 * 1024;

const FILE_PROCESSING_POLL_INTERVAL_MS = 2000;
const FILE_PROCESSING_MAX_WAIT_MS = 30000;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST만 지원합니다." });
    return;
  }

  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정돼있지 않습니다. (Vercel 환경변수 확인 필요)" });
    return;
  }

  const { message_id, question } = req.body || {};
  const messageId = Number(message_id);
  const trimmedQuestion = typeof question === "string" ? question.trim() : "";

  if (!Number.isFinite(messageId)) {
    res.status(400).json({ error: "message_id가 올바르지 않습니다." });
    return;
  }
  if (!trimmedQuestion) {
    res.status(400).json({ error: "질문을 입력해주세요." });
    return;
  }
  if (trimmedQuestion.length > MAX_QUESTION_LENGTH) {
    res.status(400).json({ error: `질문이 너무 깁니다 (최대 ${MAX_QUESTION_LENGTH}자).` });
    return;
  }

  const proto = req.headers["x-forwarded-proto"] || "https";
  const base = `${proto}://${req.headers.host}`;

  try {
    // 1) manifest.json에서 이 파일 정보를 찾는다.
    const manifestRes = await fetch(`${base}/manifest.json`, { cache: "no-store" });
    if (!manifestRes.ok) throw new Error("manifest.json을 불러오지 못했습니다.");
    const manifest = await manifestRes.json();
    const file = (manifest.files || []).find((f) => f.message_id === messageId);

    if (!file) {
      res.status(404).json({ error: "해당 자료를 찾을 수 없습니다." });
      return;
    }

    const pdfUrl = file.download_url || (file.stored_as ? `${base}/files/${encodeURIComponent(file.stored_as)}` : null);
    if (!pdfUrl) {
      res.status(404).json({ error: "원본 파일 위치를 찾을 수 없습니다." });
      return;
    }

    // 2) 크기 확인 (실제로 다 받기 전에 먼저 걸러서, 큰 파일 다운로드에 시간
    //    낭비하지 않고 바로 안내한다).
    const knownSize = file.size_bytes || 0;
    if (knownSize > MAX_PDF_BYTES) {
      res.status(413).json({
        error: `이 자료는 ${(knownSize / (1024 * 1024)).toFixed(0)}MB로 너무 커서 지금은 AI 질문에 쓸 수 없어요 (현재 한도: ${MAX_PDF_BYTES / (1024 * 1024)}MB).`,
      });
      return;
    }

    // 3) 원본 PDF를 서버(이 함수)에서 직접 받아온다. (브라우저가 아니므로 CORS 무관)
    const pdfRes = await fetch(pdfUrl);
    if (!pdfRes.ok) throw new Error("원본 PDF를 받아오지 못했습니다.");
    const pdfBuffer = Buffer.from(await pdfRes.arrayBuffer());

    // 4) Gemini Files API에 업로드 (resumable upload 프로토콜: 시작 요청 -> 업로드 요청)
    const startRes = await fetch(
      `https://generativelanguage.googleapis.com/upload/v1beta/files?key=${apiKey}`,
      {
        method: "POST",
        headers: {
          "X-Goog-Upload-Protocol": "resumable",
          "X-Goog-Upload-Command": "start",
          "X-Goog-Upload-Header-Content-Length": String(pdfBuffer.length),
          "X-Goog-Upload-Header-Content-Type": "application/pdf",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ file: { display_name: file.filename } }),
      }
    );
    if (!startRes.ok) {
      const detail = await startRes.text();
      throw new Error(`Gemini 업로드 세션 시작 실패: ${detail}`);
    }
    const uploadUrl = startRes.headers.get("x-goog-upload-url");
    if (!uploadUrl) throw new Error("Gemini 업로드 URL을 받지 못했습니다.");

    const uploadRes = await fetch(uploadUrl, {
      method: "POST",
      headers: {
        "Content-Length": String(pdfBuffer.length),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
      },
      body: pdfBuffer,
    });
    const uploadData = await uploadRes.json();
    if (!uploadRes.ok || !uploadData.file) {
      throw new Error(`Gemini 파일 업로드 실패: ${uploadData?.error?.message || JSON.stringify(uploadData)}`);
    }

    let fileState = uploadData.file.state;
    const fileName = uploadData.file.name;
    const fileUri = uploadData.file.uri;
    const fileMimeType = uploadData.file.mimeType || "application/pdf";

    // 업로드 직후 Gemini가 파일을 내부적으로 처리(PROCESSING) 중일 수 있어서,
    // ACTIVE가 될 때까지 잠깐 폴링한다 (최대 30초).
    let waited = 0;
    while (fileState === "PROCESSING" && waited < FILE_PROCESSING_MAX_WAIT_MS) {
      await sleep(FILE_PROCESSING_POLL_INTERVAL_MS);
      waited += FILE_PROCESSING_POLL_INTERVAL_MS;
      const checkRes = await fetch(`https://generativelanguage.googleapis.com/v1beta/${fileName}?key=${apiKey}`);
      const checkData = await checkRes.json();
      fileState = checkData.state;
    }
    if (fileState !== "ACTIVE") {
      throw new Error("Gemini가 파일 처리를 완료하지 못했습니다. 잠시 후 다시 시도해주세요.");
    }

    // 5) 질문과 함께 generateContent 호출.
    const parts = [
      {
        text:
          "다음 PDF 자료를 근거로 사용자의 질문에 한국어로 정확하고 간결하게 답변하세요. " +
          "스캔된 이미지 위주의 문서일 수 있으니 내용을 잘 읽고 답변하고, 답을 찾을 수 없으면 " +
          "모른다고 솔직히 말하세요.",
      },
      { file_data: { mime_type: fileMimeType, file_uri: fileUri } },
      { text: `질문: ${trimmedQuestion}` },
    ];

    const geminiRes = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${apiKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contents: [{ role: "user", parts }] }),
      }
    );
    const geminiData = await geminiRes.json();
    if (!geminiRes.ok) {
      const detail = geminiData?.error?.message || JSON.stringify(geminiData);
      throw new Error(`Gemini API 오류: ${detail}`);
    }

    const answer = geminiData?.candidates?.[0]?.content?.parts?.map((p) => p.text || "").join("") || "";
    if (!answer) {
      res.status(502).json({ error: "AI가 답변을 생성하지 못했습니다. 다시 시도해주세요." });
      return;
    }

    res.status(200).json({ answer });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message || "알 수 없는 오류가 발생했습니다." });
  }
};

// 다운로드 + Gemini 업로드 + 답변 생성까지 다 끝나려면 기본 10초로는 부족할 수
// 있어서 넉넉하게 잡는다. Vercel Hobby(무료) 플랜의 최대치가 60초.
// Pro 플랜이면 최대 800초까지 늘릴 수 있으니, 큰 파일 때문에 자주 타임아웃되면
// 플랜을 올리고 이 값과 MAX_PDF_BYTES를 함께 올리면 된다.
module.exports.config = { maxDuration: 60 };
