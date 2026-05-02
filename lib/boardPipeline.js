import { promises as fs } from "node:fs";
import path from "node:path";
import OpenAI, { toFile } from "openai";
import * as XLSX from "xlsx";

const SYSTEM_MESSAGE =
  "You are a precise board-deck analysis assistant. Follow output format exactly and avoid extra text.";

const PROMPT_DIR = path.join(process.cwd(), "prompts", "agents");

function renderTemplate(template, values) {
  let rendered = template;
  for (const [key, value] of Object.entries(values)) {
    rendered = rendered.replaceAll(`{{${key}}}`, value);
  }
  return rendered;
}

function extractResponseText(response) {
  if (typeof response?.output_text === "string" && response.output_text.trim()) {
    return response.output_text.trim();
  }

  const chunks = [];
  for (const item of response?.output || []) {
    for (const content of item?.content || []) {
      if (typeof content?.text === "string" && content.text.trim()) {
        chunks.push(content.text.trim());
      }
    }
  }

  return chunks.join("\n").trim();
}

function parseJsonOutput(raw) {
  const text = raw.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");

  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") return parsed;
  } catch {}

  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start !== -1 && end > start) {
    const candidate = text.slice(start, end + 1);
    const parsed = JSON.parse(candidate);
    if (parsed && typeof parsed === "object") return parsed;
  }

  throw new Error("Could not parse JSON output from model.");
}

function countWords(text) {
  return (text.match(/\b[\w'-]+\b/g) || []).length;
}

function parseRetrySeconds(message) {
  const m = message.match(/Please try again in ([0-9]+(?:\.[0-9]+)?)s/i);
  if (!m) return 10;
  return Number.parseFloat(m[1]) + 1;
}

async function sleep(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function callModel({ client, model, prompt, fileItems, maxRetries = 4 }) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await client.responses.create({
        model,
        input: [
          {
            role: "system",
            content: [{ type: "input_text", text: SYSTEM_MESSAGE }]
          },
          {
            role: "user",
            content: [...fileItems, { type: "input_text", text: prompt }]
          }
        ]
      });

      const text = extractResponseText(response);
      if (!text) throw new Error("Model returned empty output.");
      return text;
    } catch (err) {
      const status = err?.status || err?.code;
      const bodyMessage = err?.error?.message || err?.message || "";
      const isRetryable = String(status) === "429" || /rate limit|timeout|network/i.test(bodyMessage);

      if (!isRetryable || attempt === maxRetries) {
        throw err;
      }

      const wait = parseRetrySeconds(bodyMessage);
      await sleep(wait * 1000);
    }
  }

  throw new Error("Failed after retries.");
}

function compactText(raw, maxChars) {
  const normalized = raw.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, maxChars).trim()}\n[TRUNCATED]`;
}

function xlsxToText(arrayBuffer) {
  const workbook = XLSX.read(Buffer.from(arrayBuffer), { type: "buffer" });
  const sections = [];

  for (const sheetName of workbook.SheetNames) {
    const sheet = workbook.Sheets[sheetName];
    const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: "" });
    const lines = rows
      .map((row) => row.map((cell) => String(cell).trim()).filter(Boolean).join(" | "))
      .filter(Boolean);

    sections.push(`Sheet: ${sheetName}\n${lines.join("\n")}`);
  }

  return sections.join("\n\n---\n\n");
}

function textToDeck(fileName, text) {
  return `Slide 1\nTitle: ${fileName}\nText:\n${text}`;
}

async function prepareMaterial({ client, file, role }) {
  const name = file.name || `${role}.dat`;
  const lower = name.toLowerCase();
  const fileItems = [];
  const uploadedFileIds = [];

  if (lower.endsWith(".pdf")) {
    const buffer = Buffer.from(await file.arrayBuffer());
    const uploaded = await client.files.create({
      purpose: "user_data",
      file: await toFile(buffer, name, { type: file.type || "application/pdf" })
    });

    fileItems.push({ type: "input_file", file_id: uploaded.id });
    uploadedFileIds.push(uploaded.id);

    return {
      deckText: `(The ${role} material is attached as PDF file: ${name}. Use the attached file as the source of truth.)`,
      fileItems,
      uploadedFileIds,
      sourceType: "pdf"
    };
  }

  if (lower.endsWith(".xlsx") || lower.endsWith(".xls")) {
    const text = compactText(xlsxToText(await file.arrayBuffer()), 18000);
    return {
      deckText: textToDeck(name, text),
      fileItems,
      uploadedFileIds,
      sourceType: "xlsx"
    };
  }

  if (lower.endsWith(".txt") || lower.endsWith(".md") || lower.endsWith(".csv") || lower.endsWith(".json")) {
    const text = compactText(await file.text(), 18000);
    return {
      deckText: textToDeck(name, text),
      fileItems,
      uploadedFileIds,
      sourceType: "text"
    };
  }

  throw new Error(`Unsupported file type for ${name}. Use PDF, XLSX, TXT, CSV, MD, or JSON.`);
}

async function loadPrompt(name) {
  const fullPath = path.join(PROMPT_DIR, name);
  return fs.readFile(fullPath, "utf8");
}

export async function runBoardPipeline({ primaryFile, contextFile, factor4Name, factor5Name, model }) {
  if (!process.env.OPENAI_API_KEY) {
    throw new Error("OPENAI_API_KEY is not set.");
  }

  const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
  const uploadedIds = [];

  const relevanceTemplate = await loadPrompt("relevance_agent.md");
  const metricsTemplate = await loadPrompt("metrics_agent.md");
  const contextTemplate = await loadPrompt("context_agent.md");
  const synthesisTemplate = await loadPrompt("synthesis_agent.md");

  try {
    const primary = await prepareMaterial({ client, file: primaryFile, role: "primary" });
    uploadedIds.push(...primary.uploadedFileIds);

    let context = {
      deckText: "(No secondary context deck provided.)",
      fileItems: [],
      uploadedFileIds: [],
      sourceType: "none"
    };

    if (contextFile && contextFile.size > 0) {
      context = await prepareMaterial({ client, file: contextFile, role: "secondary context" });
      uploadedIds.push(...context.uploadedFileIds);
    }

    const sharedFileItems = [...primary.fileItems, ...context.fileItems];

    const relevancePrompt = renderTemplate(relevanceTemplate, {
      deck_text: primary.deckText,
      context_deck_text: context.deckText
    });

    const relevanceRaw = await callModel({
      client,
      model,
      prompt: relevancePrompt,
      fileItems: sharedFileItems,
      maxRetries: 5
    });
    const relevanceJson = parseJsonOutput(relevanceRaw);

    const selectedSlidesJson = JSON.stringify(relevanceJson?.selected_slides || [], null, 2);

    const metricsPrompt = renderTemplate(metricsTemplate, {
      factor_4_name: factor4Name,
      factor_5_name: factor5Name,
      selected_slides_json: selectedSlidesJson,
      deck_text: primary.deckText,
      context_deck_text: context.deckText
    });

    const metricsRaw = await callModel({
      client,
      model,
      prompt: metricsPrompt,
      fileItems: sharedFileItems,
      maxRetries: 5
    });
    const metricsJson = parseJsonOutput(metricsRaw);

    const contextPrompt = renderTemplate(contextTemplate, {
      selected_slides_json: selectedSlidesJson,
      deck_text: primary.deckText,
      context_deck_text: context.deckText
    });

    const contextRaw = await callModel({
      client,
      model,
      prompt: contextPrompt,
      fileItems: sharedFileItems,
      maxRetries: 5
    });
    const contextJson = parseJsonOutput(contextRaw);

    const synthesisPrompt = renderTemplate(synthesisTemplate, {
      factor_4_name: factor4Name,
      factor_5_name: factor5Name,
      relevance_json: JSON.stringify(relevanceJson, null, 2),
      metrics_json: JSON.stringify(metricsJson, null, 2),
      context_json: JSON.stringify(contextJson, null, 2)
    });

    let finalParagraph = await callModel({
      client,
      model,
      prompt: synthesisPrompt,
      fileItems: sharedFileItems,
      maxRetries: 5
    });

    if (countWords(finalParagraph) < 100 || countWords(finalParagraph) > 150) {
      finalParagraph = await callModel({
        client,
        model,
        prompt:
          synthesisPrompt +
          "\n\nRevision request: rewrite in 100-150 words, one paragraph only, no bullets or headings.",
        fileItems: sharedFileItems,
        maxRetries: 5
      });
    }

    return {
      model,
      factor_4_name: factor4Name,
      factor_5_name: factor5Name,
      primary_source_type: primary.sourceType,
      context_source_type: context.sourceType,
      relevance: relevanceJson,
      metrics: metricsJson,
      context: contextJson,
      final_paragraph: finalParagraph.trim(),
      final_word_count: countWords(finalParagraph)
    };
  } finally {
    await Promise.all(
      uploadedIds.map(async (id) => {
        try {
          await client.files.del(id);
        } catch {
          // Ignore cleanup failure.
        }
      })
    );
  }
}
