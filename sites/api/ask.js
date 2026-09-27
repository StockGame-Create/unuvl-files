// api/ask.js
//
// "파일 선택 후 질문하기" AI 기능의 백엔드 (대화형/멀티턴 버전).
//
// 동작 방식 (미리 렌더링 안 함, 질문할 때만 그 파일 하나를 처리):
// 1) manifest.json에서 선택된 파일의 원본 PDF 위치(GitHub Release 다운로드 URL)를 찾는다.
// 2) 서버(이 함수) 쪽에서 그 PDF를 받아온다. 브라우저가 아니라 서버가 받는 거라
//    CORS 문제가 없다 (브라우저 fetch만 objects.githubusercontent.com의 CORS에 막힌다).
// 3) 받은 PDF를 Gemini Files API에 그대로 업로드한다. Gemini는 PDF를 내부적으로
//    페이지 이미지로 변환해서 이해하기 때문에, 우리가 따로 페이지를 이미지로
//    렌더링해둘 필요가 없다 (스캔 이미지 위주인 자료에도 그대로 통함).
// 4) 업로드된 파일 URI + 대화 기록(history) + 새 질문을 generateContent에
//    같이 넘겨서 답변을 받는다.
//
// 멀티턴(대화 기억) 지원: 프론트엔드가 매 요청마다 이전 턴들의 { role, text }
// 배열을 history로 같이 보내주면, 그걸 그대로 Gemini의 contents에 이어붙여서
// "이 대화 맥락을 기억하는" 것처럼 동작한다. 서버 자체는 상태를 저장하지 않는다
// (완전한 stateless 서버리스 함수 - 기억은 클라이언트가 들고 있다가 매번 다시 보내는 것).
//
// 파일 재업로드 방지: 프론트엔드가 이전 응답에서 받은 file_state
// ({ name, uri, mimeType })를 다음 질문에 같이 보내주면, 같은 파일을 다시
// 다운로드/업로드하지 않고 그 참조를 그대로 재사용한다. Gemini Files API에
// 올라간 파일은 약 48시간 동안 유효하므로, 하나의 대화 세션 안에서는 첫 질문만
// 느리고 이후 질문들은 훨씬 빠르다. file_state가 없거나 재사용에 실패하면
// (예: 시간이 많이 지나 파일이 만료됨) 처음부터 다시 다운로드/업로드한다.
//
// 필요한 환경변수 (Vercel 프로젝트 설정 > Environment Variables에 추가해야 함.
// GitHub Secrets의 GEMINI_API_KEY와는 별개의 저장소이니 반드시 여기에도 등록할 것):
//   GEMINI_API_KEY
//
// 요청 형식: POST {
//   message_id: number,
//   question: string,
//   history?: Array<{ role: "user"|"assistant", text: string }>,
//   file_state?: { name: string, uri: string, mimeType: string } | null
// }
// 응답 형식: { answer: string, file_state: {...} } 또는 { error: string }

const GEMINI_MODEL = "gemini-3.8-flash"; // gemini-2.5-flash가 신규 사용자에게 막혀서 교체 (2026-09), PDF 네이티브 이해 지원
const MAX_QUESTION_LENGTH = 1000;
const MAX_HISTORY_TURNS = 40; // user+assistant 메시지 합쳐서 최대 개수 (그 이상은 오래된 것부터 자름)

// Vercel 서버리스 함수 실행시간 한도(아래 config.maxDuration) 안에 "다운로드 +
// Gemini 업로드 + 답변 생성"이 다 끝나야 하므로, 너무 큰 파일은 아예 거절한다.
// Hobby(무료) 플랜은 함수 실행시간이 최대 60초라 넉넉하게 잡기 어렵다 - 일단
// 100MB로 시작하고, 실제로 타임아웃이 잦으면 더 낮추면 된다. Vercel Pro면
// maxDuration을 최대 800초까지 늘릴 수 있어서 이 값도 같이 올릴 수 있다.
// (file_state를 재사용하는 2번째 턴부터는 이 다운로드/업로드 과정 자체가
// 생략되므로 훨씬 여유있게 끝난다.)
const MAX_PDF_BYTES = 100 * 1024 * 1024;

const FILE_PROCESSING_POLL_INTERVAL_MS = 2000;
const FILE_PROCESSING_MAX_WAIT_MS = 30000;

// 학생들이 올리는 학습자료(모의고사/문제집 등)는 보통 "문제"와 "해설"이
// 나뉘어 있는 경우가 많다. 답변 품질을 위해 이 두 영역을 먼저 구분해서
// 찾아보라고 명시적으로 지시한다. 또한 수식은 LaTeX로, 전체 답변은
// 마크다운으로 정리해서 프론트엔드가 예쁘게 렌더링할 수 있게 한다.
const SYSTEM_INSTRUCTION = `당신은 학생들이 업로드한 학습자료(PDF)를 근거로 답변하는 한국어 AI 튜터입니다.

자료를 분석하고 답변할 때 반드시 다음을 지키세요:

1) 자료를 읽을 때 먼저 전체를 훑어보면서 "문제"라고 표시되었거나 문항 번호가
   매겨진 "문제 영역"과, "해설"이라고 표시되었거나 정답/풀이가 적힌
   "해설 영역"이 있는지 확인하세요. 이 두 영역이 존재하면 그것을 최우선
   근거로 삼아 답변하세요 (예: 몇 번 문제인지 먼저 문제 영역에서 정확히
   찾고, 그 다음 해설 영역에서 대응하는 풀이/정답을 찾아 답변에 반영).
2) 수식, 기호, 화학식 등이 필요하면 반드시 LaTeX 표기를 사용하세요:
   문장 중간에 들어가는 인라인 수식은 $...$ 로, 독립된 한 줄 수식은
   $$...$$ 로 감싸세요. 일반 텍스트에 유니코드 특수기호를 남발하지 말고
   수식은 LaTeX로 표현하세요.
3) 마크다운 문법(목록, 굵게, 표, 코드블록 등)을 적절히 사용해서 읽기 쉽게
   정리하세요. 다만 과하게 화려한 서식은 피하고 필요한 곳에만 쓰세요.
4) 이전 대화 맥락이 있다면 그것을 기억하고 자연스럽게 이어지는 대화체로
   답하세요. "아까 물어본 것과 이어서" 같은 표현도 자연스럽게 받아들이세요.
5) 자료 안에서 답을 찾을 수 없으면 추측하지 말고 모른다고 솔직히 말하세요.
6) 답변은 한국어로, 정확하고 간결하게 작성하세요.`;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 이미 업로드된 Gemini 파일(file_state)이 아직 유효한지 가볍게 확인한다.
// 실패하면 null을 반환해서 호출부가 새로 업로드하도록 유도한다.
async function tryReuseFileState(fileState, apiKey) {
  if (!fileState || !fileState.name || !fileState.uri || !fileState.mimeType) return null;
  try {
    const checkRes = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/${fileState.name}?key=${apiKey}`
    );
    if (!checkRes.ok) return null;
    const checkData = await checkRes.json();
    if (checkData.state !== "ACTIVE") return null;
    return fileState;
  } catch {
    return null;
  }
}

// manifest.json에서 파일 정보를 찾아 다운로드한 뒤 Gemini Files API에 업로드하고,
// { name, uri, mimeType }를 반환한다.
async function uploadFreshFile(messageId, apiKey, base) {
  const manifestRes = await fetch(`${base}/manifest.json`, { cache: "no-store" });
  if (!manifestRes.ok) throw new Error("manifest.json을 불러오지 못했습니다.");
  const manifest = await manifestRes.json();
  const file = (manifest.files || []).find((f) => f.message_id === messageId);

  if (!file) {
    const err = new Error("해당 자료를 찾을 수 없습니다.");
    err.statusCode = 404;
    throw err;
  }

  const pdfUrl = file.download_url || (file.stored_as ? `${base}/files/${encodeURIComponent(file.stored_as)}` : null);
  if (!pdfUrl) {
    const err = new Error("원본 파일 위치를 찾을 수 없습니다.");
    err.statusCode = 404;
    throw err;
  }

  const knownSize = file.size_bytes || 0;
  if (knownSize > MAX_PDF_BYTES) {
    const err = new Error(
      `이 자료는 ${(knownSize / (1024 * 1024)).toFixed(0)}MB로 너무 커서 지금은 AI 질문에 쓸 수 없어요 (현재 한도: ${MAX_PDF_BYTES / (1024 * 1024)}MB).`
    );
    err.statusCode = 413;
    throw err;
  }

  const pdfRes = await fetch(pdfUrl);
  if (!pdfRes.ok) throw new Error("원본 PDF를 받아오지 못했습니다.");
  const pdfBuffer = Buffer.from(await pdfRes.arrayBuffer());

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

  return { name: fileName, uri: fileUri, mimeType: fileMimeType };
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

  const { message_id, question, history, file_state } = req.body || {};
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

  // history는 신뢰할 수 없는 클라이언트 입력이므로 형태를 검증하고, 너무 길면
  // (토큰/시간 낭비 방지 + 악용 방지) 최근 것만 남기고 자른다.
  let safeHistory = [];
  if (Array.isArray(history)) {
    safeHistory = history
      .filter((h) => h && typeof h.text === "string" && (h.role === "user" || h.role === "assistant"))
      .map((h) => ({ role: h.role, text: h.text.slice(0, 4000) }))
      .slice(-MAX_HISTORY_TURNS);
  }

  const proto = req.headers["x-forwarded-proto"] || "https";
  const base = `${proto}://${req.headers.host}`;

  try {
    // 1) 이전 턴에서 받은 file_state가 아직 유효하면 재사용, 아니면 새로 업로드.
    let fileState = await tryReuseFileState(file_state, apiKey);
    if (!fileState) {
      fileState = await uploadFreshFile(messageId, apiKey, base);
    }

    // 2) 대화 기록 + 새 질문으로 contents 구성.
    //    파일은 매 턴 새 질문에 같이 첨부한다 (같은 file_uri를 참조하는 것이라
    //    실제 바이너리를 매번 다시 보내는 게 아니고, 문맥 유지가 더 안정적이다).
    const contents = safeHistory.map((h) => ({
      role: h.role === "assistant" ? "model" : "user",
      parts: [{ text: h.text }],
    }));
    contents.push({
      role: "user",
      parts: [
        { file_data: { mime_type: fileState.mimeType, file_uri: fileState.uri } },
        { text: trimmedQuestion },
      ],
    });

    const geminiRes = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${apiKey}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          system_instruction: { parts: [{ text: SYSTEM_INSTRUCTION }] },
          contents,
        }),
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

    res.status(200).json({ answer, file_state: fileState });
  } catch (err) {
    console.error(err);
    res.status(err.statusCode || 500).json({ error: err.message || "알 수 없는 오류가 발생했습니다." });
  }
};

// 다운로드 + Gemini 업로드 + 답변 생성까지 다 끝나려면 기본 10초로는 부족할 수
// 있어서 넉넉하게 잡는다. Vercel Hobby(무료) 플랜의 최대치가 60초.
// (file_state를 재사용하는 2번째 턴부터는 다운로드/업로드가 생략되어 훨씬
// 빠르게 끝난다.) Pro 플랜이면 최대 800초까지 늘릴 수 있으니, 큰 파일 때문에
// 자주 타임아웃되면 플랜을 올리고 이 값과 MAX_PDF_BYTES를 함께 올리면 된다.
module.exports.config = { maxDuration: 60 };
