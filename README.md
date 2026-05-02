# BoardBaby

BoardBaby is a Vercel-ready tool for summarizing board materials into one investor-ready paragraph.

It supports exactly two inputs:
- Deck (required)
- Financials (required)

Supported upload types:
- `.pdf`
- `.xlsx` / `.xls`
- `.txt` / `.csv` / `.md` / `.json`

API output:
- One field: `paragraph`

## Local Run

1. Install dependencies:

```bash
npm install
```

2. Set your API key:

```bash
export OPENAI_API_KEY="sk-..."
```

3. Start the app:

```bash
npm run dev
```

Open `http://localhost:3000`.

## Deploy To Vercel

1. Push this repo to GitHub.
2. In Vercel, create a new project from the repo.
3. Add environment variable:

- `OPENAI_API_KEY` = your key

4. Deploy.

No extra build config is required.

## Notes

- API route: `app/api/summarize/route.js` (`POST`, 2 inputs -> 1 paragraph output)
- Pipeline logic: `lib/boardPipeline.js`
- Prompts: `prompts/agents/*.md`
- The API uses retry/backoff for transient rate limits.
