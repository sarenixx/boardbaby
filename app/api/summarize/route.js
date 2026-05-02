import { NextResponse } from "next/server";
import { runBoardPipeline } from "@/lib/boardPipeline";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 300;

export async function POST(request) {
  try {
    const form = await request.formData();

    const deckFile = form.get("deck_file");
    const financialsFile = form.get("financials_file");
    const factor4Name = String(form.get("factor_4_name") || "").trim();
    const factor5Name = String(form.get("factor_5_name") || "").trim();
    const model = String(form.get("model") || "gpt-4.1-mini").trim();

    if (!deckFile || typeof deckFile === "string") {
      return NextResponse.json({ error: "Deck file is required." }, { status: 400 });
    }

    if (!financialsFile || typeof financialsFile === "string") {
      return NextResponse.json({ error: "Financials file is required." }, { status: 400 });
    }

    if (!factor4Name || !factor5Name) {
      return NextResponse.json(
        { error: "Both factor_4_name and factor_5_name are required." },
        { status: 400 }
      );
    }

    const result = await runBoardPipeline({
      primaryFile: deckFile,
      contextFile: financialsFile,
      factor4Name,
      factor5Name,
      model
    });

    return NextResponse.json({ paragraph: result.final_paragraph });
  } catch (error) {
    const message = error?.error?.message || error?.message || "Unexpected server error.";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
