# WB YouTube Job Alert Agent 🔔

An automated, fully-free system that monitors **143 YouTube channels** twice daily for West Bengal government job updates and sends clean Telegram alerts — with on-demand deep extraction.

---

## How It Works

```
06:00 AM / 06:00 PM IST (automatic)
        ↓
Check all 143 channels (new videos only)
        ↓
Keyword filter → AI confirms relevance
        ↓
Group duplicates → ONE event per exam
        ↓
Simple Telegram alert

  🟢 SSC CHSL 2026 — Application Started
  [✅ EXTRACT FULL INFORMATION] [❌ SKIP]
        ↓ (you press EXTRACT)
Fetch transcript + description + official PDF
        ↓
AI extracts 30+ fields (dates, fees, eligibility, photo requirements…)
        ↓
Complete A–Z information in Telegram
```

---

## Monitored Event Types

| Event | Alert Emoji | Trigger |
|-------|------------|---------|
| Application Started | 🟢 | Form is open NOW |
| Application Starting Soon | 🟡 | Concrete start date given |
| Admit Card Released | 🔵 | Hall ticket available |
| Result Declared | 🏆 | Result published |

**Everything else is filtered out:** mock tests, preparation tips, coaching ads, exam analysis, how-to-fill tutorials, motivation videos.

---

## Complete Setup Guide

### Prerequisites
- GitHub account (free)
- Google account (for YouTube API)
- Telegram account

---

### Step 1 — Fork / Create the Repository

Go to [github.com/new](https://github.com/new) and create a repository named:
```
wb-youtube-job-alert-agent
```

> **Recommendation:** Make it **Public** — GitHub Actions is completely free and unlimited for public repositories.  
> For a **Private** repository, you get 2,000 minutes/month free (still sufficient at ~600 min/month usage).

Upload all files from this project to the repository.

---

### Step 2 — Add Your 143 Channels

Open `channels_raw.txt` and paste your channel list. One per line. Accepted formats:

```
https://www.youtube.com/@ChannelHandle
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxxxx
@ChannelHandle
UCxxxxxxxxxxxxxxxxxxxxxxxx
```

Lines starting with `#` are ignored.

---

### Step 3 — Set Up YouTube Data API

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a new project → name it `WB YouTube Job Alert`
3. Go to **APIs & Services → Library**
4. Search **YouTube Data API v3** → Enable
5. Go to **APIs & Services → Credentials → Create Credentials → API Key**
6. Click **Restrict Key** → Under **API restrictions** → Select **YouTube Data API v3**
7. Copy the API key

> **Note:** IP restriction does NOT work with GitHub Actions (IPs change every run). Use API restriction only.

**Daily quota:** 10,000 units/day. This system uses ~326 units/day (96.7% headroom).

---

### Step 4 — Set Up OpenRouter

1. Go to [openrouter.ai/keys](https://openrouter.ai/keys)
2. Sign up / log in
3. Create an API key
4. Copy the key

**Free tier:** 50 requests/day. This system uses ~2–5 per monitoring run + 1–2 per EXTRACT press.

---

### Step 5 — Set Up Telegram Bot

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., `WB Job Alert`)
4. Choose a username (e.g., `wbjobalert_bot`)
5. BotFather gives you: `TELEGRAM_BOT_TOKEN` — copy it
6. Add your bot to your personal Telegram chat or a private group
7. Send `/start` to the bot

---

### Step 6 — Add GitHub Secrets

Go to your repository → **Settings → Secrets and variables → Actions → New repository secret**

Create these 4 secrets:

| Secret Name | Value |
|------------|-------|
| `YOUTUBE_API_KEY` | From Step 3 |
| `OPENROUTER_API_KEY` | From Step 4 |
| `TELEGRAM_BOT_TOKEN` | From Step 5 |
| `TELEGRAM_CHAT_ID` | Find in Step 7 |

---

### Step 7 — Find Your Telegram Chat ID

After adding the bot token secret:

1. Go to **Actions → Get Telegram Chat ID → Run workflow**
2. Check the workflow logs
3. Copy your Chat ID (looks like `123456789` for personal chats or `-1001234567890` for groups)
4. Add it as `TELEGRAM_CHAT_ID` secret

> If no chats appear: send a message to your bot in Telegram first, then re-run the workflow.

---

### Step 8 — Build the Channel List

1. Go to **Actions → Build Channel List → Run workflow**
2. Wait ~5–10 minutes for all 143 channels to resolve
3. Check the workflow summary for how many resolved successfully
4. `channels.csv` will be automatically committed to the repository

---

### Step 9 — Bootstrap (First Run)

**This step is critical.** Without it, the system will alert on ALL existing videos.

1. Go to **Actions → Monitor YouTube Channels → Run workflow**
2. Set **Bootstrap mode** = ✅ checked
3. Run it
4. Wait for completion — it will mark all current videos as "already seen"

After bootstrap completes:
- Open `config/settings.json`
- Change `"bootstrap_mode": true` → `"bootstrap_mode": false`
- Commit the change

---

### Step 10 — Test the System

1. Go to **Actions → Test System → Run workflow**
2. Select **all** to test everything
3. Check each subsystem passes ✅
4. You should receive a **🧪 TEST ALERT** in Telegram

---

### Step 11 — Go Live 🚀

The system will now automatically run at:
- **06:00 AM IST** every day
- **06:00 PM IST** every day

You can also trigger it manually any time via **Actions → Monitor YouTube Channels → Run workflow**.

---

## Configuration

### `config/settings.json` — Main control file

```json
{
  "modes": {
    "bootstrap_mode": false,    // ← Set true ONLY for first run
    "test_mode": false          // ← Set true to test without real alerts
  },
  "confirmation": {
    "min_sources": 1,           // ← 1 = alert on single strong source
    "high_confidence_threshold": 0.85
  },
  "full_information": {
    "fees": true,               // ← Toggle each extraction field
    "photo_requirements": true,
    "application_steps": true
    // ... etc
  }
}
```

### `config/keywords.json` — Filter rules

- `include_patterns` — words that trigger candidate selection
- `exclude_patterns` — words that indicate irrelevant content  
- `exam_name_normalizations` — canonical exam names for deduplication

---

## What "EXTRACT FULL INFORMATION" Returns

```
🟢 SSC CHSL 2026 — Application Started

📅 Important Dates
• Application Start: 21 September 2026
• Last Date to Apply: 18 October 2026
• Fee Payment Last Date: 19 October 2026
• Correction Window: 21–23 October 2026
• Exam Date: Not found in available source
• Admit Card Date: Not found in available source

📋 Post & Vacancy
• Organization: Staff Selection Commission
• Post Name: Combined Higher Secondary Level
• Total Vacancy: Not found in available source

🎓 Eligibility
• Qualification: 12th Pass
• Age Limit: 18–27 years
• Age Relaxation: OBC 3 years, SC/ST 5 years

💰 Application Fee
• General: ₹100
• OBC: ₹100
• SC: ₹0 (Exempt)
• ST: ₹0 (Exempt)
• Female: ₹0 (Exempt)

📄 Documents Required
  — Aadhaar/valid ID proof
  — 10th/12th marksheet
  — Recent photograph
  — Signature
  — Category certificate (if applicable)

🖼️ Photo Upload
• Format: JPG/JPEG
• File Size: 20–50 KB
• Dimensions: 3.5cm × 4.5cm
• Background: White

✍️ Signature Upload
• Format: JPG/JPEG
• File Size: 10–20 KB

📝 Application Steps
1. Visit ssc.nic.in
2. Register with email and mobile
3. Login with registration number
4. Fill personal details
5. Fill educational details
6. Upload photo and signature
7. Pay application fee
8. Submit application
9. Download confirmation page

🌐 Official Links
Website: https://ssc.nic.in

🎥 Sources
  [WB Job Update](https://youtube.com/...)

Information confidence: High
Official document found: Yes ✅
```

> **Important:** Fields marked "Not found in available source" were genuinely absent from all fetched sources. The system never invents information.

---

## Resource Usage

| Resource | Usage | Limit | Headroom |
|----------|-------|-------|----------|
| GitHub Actions minutes | ~600/month | ∞ (public repo) | ∞ |
| YouTube API quota | ~326 units/day | 10,000/day | 97% |
| OpenRouter AI requests | 2–10/day | 50/day | 80%+ |

---

## Project Structure

```
wb-youtube-job-alert-agent/
├── .github/workflows/
│   ├── monitor.yml              # Main monitoring (06:00 + 18:00 IST)
│   ├── telegram_commands.yml    # Button press handler (every 10 min)
│   ├── build_channels.yml       # One-time channel list builder
│   ├── get_telegram_chat_id.yml # Setup helper
│   └── test.yml                 # Subsystem tester
├── src/
│   ├── main.py                  # Orchestrator
│   ├── youtube.py               # YouTube API wrapper
│   ├── classifier.py            # 2-stage video classifier
│   ├── dedup.py                 # Event deduplication
│   ├── extractor.py             # Deep information extractor
│   ├── transcript.py            # YouTube caption fetcher
│   ├── official_docs.py         # Official PDF/document fetcher
│   ├── telegram.py              # Telegram Bot API wrapper
│   ├── poll_commands.py         # Telegram button handler
│   ├── build_channels_script.py # Channel resolver
│   ├── test_system.py           # Test runner
│   └── utils.py                 # Shared utilities
├── config/
│   ├── settings.json            # Main settings (edit this)
│   └── keywords.json            # Filter rules and exam names
├── data/
│   ├── state.json               # Processed video IDs + quota tracking
│   ├── events.json              # Active event registry
│   └── telegram_offset.json    # Telegram update offset
├── channels_raw.txt             # YOUR 143 channels (paste here)
├── channels.csv                 # Auto-generated by build_channels.yml
└── requirements.txt
```

---

## Troubleshooting

**No alerts arriving?**
- Check that `bootstrap_mode` is `false` in `settings.json`
- Check GitHub Actions logs for errors
- Run the `test.yml` workflow with `telegram` target

**"YOUTUBE_API_KEY not set" error?**
- Go to Settings → Secrets → verify `YOUTUBE_API_KEY` exists

**"channels.csv not found" error?**
- Run `build_channels.yml` workflow first

**Button press not triggering extraction?**
- Telegram callbacks can take up to 10 minutes to process (that's the polling interval)
- Check `telegram_commands.yml` workflow run logs

**Too many alerts / duplicate alerts?**
- Check `data/events.json` — an event already in the file won't trigger again
- If an event shows `"alert_sent": true`, it won't re-alert unless status changes

---

## Security

All API keys are stored as **GitHub Secrets** — they are never visible in code, logs, or workflow outputs. The `data/` files committed to the repo contain only video IDs and event metadata — no secrets.

---

## License

MIT License — Free to use, modify, and share.

Built for **HASAN ONLINE** — WB Government Job Assistance Service.
