# Render Deployment Guide

## Architecture

- **Backend**: Python Flask Web Service (Render Web Service)
- **Frontend**: React/Vite Static Site (Render Static Site)

---

## Backend — Render Web Service

| Setting | Value |
|---|---|
| **Root directory** | `/` (repository root) |
| **Runtime** | Python 3 |
| **Build command** | `pip install -r requirements.txt` |
| **Start command** | `gunicorn backend.api:app --bind 0.0.0.0:$PORT` |
| **Health endpoint** | `/health` |

### Environment Variables

| Variable | Required | Value |
|---|---|---|
| `AI_DIAGNOSER_API_KEY` | Yes | Your OpenRouter API key |
| `AI_DIAGNOSER_API_BASE` | Yes | `https://openrouter.ai/api/v1` |
| `AI_DIAGNOSER_MODEL` | Yes | `dots-studio/dots-3-note-preview:free` |
| `AI_DIAGNOSER_TIMEOUT` | Yes | `30` |
| `RAZORPAY_WEBHOOK_SECRET` | No | Razorpay webhook signing secret (for live webhooks) |

**Never put real API keys in DEPLOYMENT.md.** Set them in Render's Environment tab.

### When `AI_DIAGNOSER_API_KEY` is set:

- Dashboard shows **LIVE AI** badge
- Recovery decisions use real LLM diagnosis

### When `AI_DIAGNOSER_API_KEY` is absent:

- Dashboard shows **MOCK AI** badge
- Falls back to deterministic mock diagnosis
- All features still work for demo

---

## Frontend — Render Static Site

| Setting | Value |
|---|---|
| **Root directory** | `dashboard` |
| **Runtime** | Node |
| **Build command** | `npm install && npm run build` |
| **Publish directory** | `dist` |

### Environment Variables

| Variable | Required | Value |
|---|---|---|
| `VITE_API_URL` | Yes | Your deployed backend URL (e.g. `https://your-backend.onrender.com`) |

### How it works:

- **Development** (`npm run dev`): Vite proxy sends `/api/*` → `http://127.0.0.1:5001/*`
- **Production** (Render): `VITE_API_URL` is baked into the build at build time — the frontend calls the backend directly

### CORS

The Flask backend allows cross-origin requests from any HTTPS origin. No additional configuration needed.

---

## Post-Deploy Checklist

1. Backend `/health` returns `{"status": "healthy", ...}`
2. Set `VITE_API_URL` on the frontend to the backend's Render URL
3. Redeploy the frontend after setting the env var (env vars are baked into the build)
4. Click "Simulate BANK_X UPI Incident" — should show full pipeline
5. If `AI_DIAGNOSER_API_KEY` is set, verify **LIVE AI** badge appears
