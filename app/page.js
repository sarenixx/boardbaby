"use client";

import { useState } from "react";

export default function HomePage() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [paragraph, setParagraph] = useState("");

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setParagraph("");

    const form = new FormData(e.currentTarget);
    const deck = form.get("deck_file");
    const financials = form.get("financials_file");

    if (!deck || typeof deck === "string" || !deck.size) {
      setError("Deck file is required.");
      return;
    }

    if (!financials || typeof financials === "string" || !financials.size) {
      setError("Financials file is required.");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/summarize", {
        method: "POST",
        body: form
      });

      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload?.error || "Summarization failed.");
      }

      setParagraph(payload?.paragraph || "");
    } catch (err) {
      setError(err.message || "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main>
      <h1>BoardBaby MVP</h1>
      <p className="subtitle">
        Two inputs (deck + financials), one output (summary paragraph).
      </p>

      <form className="card" onSubmit={onSubmit}>
        <div className="form-grid">
          <div>
            <label htmlFor="deck_file">Deck (required)</label>
            <input id="deck_file" name="deck_file" type="file" required />
          </div>

          <div>
            <label htmlFor="financials_file">Financials (required)</label>
            <input id="financials_file" name="financials_file" type="file" required />
          </div>

          <div>
            <label htmlFor="factor_4_name">Factor 4 name</label>
            <input
              id="factor_4_name"
              name="factor_4_name"
              defaultValue="GAAP EBITDA, ADJ."
              required
            />
          </div>

          <div>
            <label htmlFor="factor_5_name">Factor 5 name</label>
            <input
              id="factor_5_name"
              name="factor_5_name"
              defaultValue="ENDING CASH BALANCE"
              required
            />
          </div>

          <div>
            <label htmlFor="model">Model</label>
            <select id="model" name="model" defaultValue="gpt-4.1-mini">
              <option value="gpt-4.1-mini">gpt-4.1-mini (recommended MVP)</option>
              <option value="gpt-4.1">gpt-4.1</option>
            </select>
          </div>

          <div className="full">
            <button type="submit" disabled={loading}>
              {loading ? "Summarizing..." : "Generate Summary"}
            </button>
          </div>
        </div>
      </form>

      {error ? (
        <div className="card result" style={{ borderColor: "#dc2626" }}>
          <strong>Error:</strong> {error}
        </div>
      ) : null}

      {paragraph ? (
        <div className="card result">
          <h3 style={{ marginTop: 0 }}>Summary Paragraph</h3>
          <p>{paragraph}</p>
        </div>
      ) : null}
    </main>
  );
}
