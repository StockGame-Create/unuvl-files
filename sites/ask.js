// api/ask.js
//
// "파일 선택 후 질문하기" AI 기능의 백엔드.
//
// 왜 이렇게 만들었나:
// - 원본 PDF는 GitHub Release에 있고 최대 1.9GB까지 갈 수 있어서, 질문 하나
//   받자고 그때그때 원본을 받아오는 건 CORS 문제(브라우저에서 직접 fetch 불가)와
//   시간/용량 문제(서버리스 함수 타임아웃) 둘 다에 걸린다.
// - 그래서 sync.py가 동기화 시점에 이미 각 PDF의 페이지들을 작은 JPEG
//   이미지로 미리 렌더링해서 이 사이트 자체에 커밋해뒀다 (sites/pages/{message_id}/001.jpg ...).
//   이 함수는 그 작은 이미지들만 받아서 Gemini(비전 모델)에 넘긴다.
//   PDF들이 대부분 스캔 이미지라, 텍스트 추출 대신 이미지를 그대로 보여주고
//   모델이 직접 읽게 하는 방식이 더 맞다.
//
// 필요한 환경변수 (Vercel 프로젝트 설정 > Environment Variables에 추가해야 함.
// GitHub Secrets의 GEMINI_API_KEY와는 별개의 저장소이니 반드시 여기에도 등록할 것):
//   GEMINI_API_KEY
//
// 요청 형식: POST { message_id: number, question: string }
// 응답 형식: { answer: string } 또는 { error: string }

const GEMINI_MODEL = "gemini-2.5-flash"; // 2026년 기준 무료 티어 기본 모델 (멀티모달 지원)
const MAX_QUESTION_LENGTH = 1000;

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

    const aiPages = file.ai_pages || 0;
    if (aiPages === 0) {
      res.status(422).json({
        error: "이 자료는 아직 AI 질문 기능을 지원하지 않습니다 (오래된 파일이거나 페이지 렌더링에 실패했습니다).",
      });
      return;
    }

    // 2) 미리 렌더링해둔 페이지 이미지들을 가져와서 base64로 변환.
    const pageNumbers = Array.from({ length: aiPages }, (_, i) => i + 1);
    const images = await Promise.all(
      pageNumbers.map(async (n) => {
        const pad = String(n).padStart(3, "0");
        const imgRes = await fetch(`${base}/pages/${messageId}/${pad}.jpg`);
        if (!imgRes.ok) return null;
        const buf = Buffer.from(await imgRes.arrayBuffer());
        return buf.toString("base64");
      })
    );
    const validImages = images.filter(Boolean);

    if (validImages.length === 0) {
      res.status(500).json({ error: "페이지 이미지를 불러오지 못했습니다." });
      return;
    }

    // 3) Gemini API 호출 (이미지 여러 장 + 질문을 한 번에 전달).
    const parts = [
      {
        text:
          "다음은 한 학습 자료(스캔 이미지)의 페이지들입니다. 이 내용을 근거로 " +
          "사용자의 질문에 한국어로 정확하고 간결하게 답변하세요. 이미지에서 답을 " +
          "찾을 수 없으면 모른다고 솔직히 말하세요.",
      },
      ...validImages.map((data) => ({
        inline_data: { mime_type: "image/jpeg", data },
      })),
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

    const truncatedNote =
      file.page_count > aiPages
        ? ` (참고: 이 자료는 총 ${file.page_count}쪽 중 처음 ${aiPages}쪽까지만 AI가 볼 수 있습니다.)`
        : "";

    res.status(200).json({ answer: answer + truncatedNote });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message || "알 수 없는 오류가 발생했습니다." });
  }
};

// Vercel Serverless Function 설정: Gemini 응답 대기 + 이미지 여러 장 fetch를
// 감안해서 기본 10초보다 길게 잡는다 (Hobby 플랜에서도 60초까지 허용됨).
module.exports.config = { maxDuration: 60 };
