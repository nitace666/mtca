# MTCA User Guide

> **Version**: MTCA v0.5 (M2.5 polish release)  
> **For**: Real users who just installed MTCA and want to start using it — no coding background required  
> **Reading time**: 5 – 30 minutes

---

## 1. Welcome: you've just opened MTCA

**MTCA in one sentence: it's a "never-loses-anything notebook" for you and your AI.**

If you use ChatGPT, Claude, or Cursor regularly, what's your biggest pain point? That conversation you had a few days ago — you can't find it anymore. That brilliant insight from three years ago — completely lost.

MTCA fixes exactly that. It's a long-term memory box that lives entirely on your computer:

- Every AI conversation you have is silently written to a local SQLite database
- Whether it's one week or three years ago, search by keyword and it finds the original text
- Your data **stays on your machine forever**, works offline, and survives even if the AI company shuts down

**How is it different from "AI notebooks" that already exist?**

| Typical AI notebook | MTCA |
|---|---|
| You have to manually organize everything | AI writes for you automatically — zero effort |
| Keyword search often misses | FTS5 full-text index + segment-level matching |
| Old content gets buried over time | Important content never "sinks" |
| Lives in the cloud — you can't touch it | A single `.db` file you can copy anywhere |

**The single most important rule (Iron Rule #9):**

> **MTCA will never forget what matters.**  
> Content you've marked "important", urgent items, deadlines you've called out — they're always there, even after 1 year or 3 years.

---

## 2. Quick install

### 2.1 What you need first

| Item | Requirement |
|---|---|
| Operating system | Windows 10+ / macOS 12+ / Ubuntu 20.04+ |
| Python | 3.10 or higher (download from [python.org](https://www.python.org/downloads/)) |
| Disk space | ≥ 2 GB free |
| Memory | ≥ 4 GB |

### 2.2 One command to install

Open a terminal (PowerShell on Windows, Terminal on Mac/Linux) and run:

```bash
pip install -e .
```

> This needs to be run inside the MTCA source directory. If you downloaded the source archive, `cd` into the unzipped folder first.

### 2.3 Verify it works

```bash
python -c "import src; print('MTCA OK')"
```

If you see `MTCA OK` printed on screen, you're set.

---

## 3. First-time use: get going in 5 minutes

### 3.1 Open the GUI

```bash
python gui/run.py
```

You'll see a window titled "MTCA — 长期记忆中间件". On first launch the window is empty (no conversations recorded yet).

![MTCA main window](docs/assets/user_guide/en_main_window.png)

The window has two main areas:

- **Top**: 5 tabs (Timeline / Detail / Knowledge Graph / Actions / 4 Quadrants)
- **Left side**: segments listed in reverse chronological order
- **Bottom status bar**: current database path

### 3.2 Write your first memory

MTCA doesn't have an "input box" — its design is: **AI tool conversations are persisted automatically**.

The simplest way to test it: run this command in your terminal to manually write a test segment:

```bash
python -m src.cli.timeline timeline --period month
```

If you see a timeline output (even empty), CLI is working.

### 3.3 Want to see real "4 quadrants"?

Open the "4 象限" tab. You'll see:

- **A yellow URGENT bar at the top**: tells you if there are overdue urgent items
- **A 2 × 2 grid**: four quadrants containing topics of different importance / urgency

![MTCA 4-quadrant view + URGENT bar](docs/assets/user_guide/en_quadrant_with_urgent.png)

If your database is empty, all four quadrants show "empty". Section 4 explains how to fill them up.

---



### 5. Try the 5-minute demo (CLI-only)

Skip the GUI and shell tools — one Python command walks through MTCA's core flow:

```bash
python examples/demo_run.py
```

You'll see 8 numbered steps with Chinese output: env check → isolated DB → 3 demo conversations → `/紧急` → `/重要` → recall → Iron Rule 9 verification → cleanup. Everything stays in `examples/__demo_db/demo.db`; your real `~/.mtca/mtca.db` is never touched.

Flags:

| Flag | What it does |
|---|---|
| `--keep-db` | keep the demo DB after the run (good for opening it in the GUI afterwards) |
| `--db /path/to/isolated.db` | write to a custom DB path (handy for tests) |

The script deletes the demo DB at exit and keeps only the most recent 3 `.bak-<timestamp>` backups.

Want to peek at what it wrote?

```bash
python examples/demo_run.py --keep-db
python -c "import sqlite3; c = sqlite3.connect('examples/__demo_db/demo.db'); \
  print(c.execute('SELECT segment_id, current_score, urgent_state FROM segments').fetchall())"
```

Smoke tests:

```bash
python -m pytest tests/test_demo.py -q
```

All 5 should pass (run / segments / URGENT / recall / Iron Rule 9).

---
## 4. Day-to-day use: the GUI

After installing MTCA, the GUI is the most common entry point. Here's how to use each of the 5 tabs.

### 4.1 Timeline (default)

Lists all segments in reverse chronological order. Each row shows:

- Time range (e.g. `13:00-13:05`)
- Topic (e.g. `下班顺路取快递`)
- Current score (`score=100`)
- State label: `[active]` / `[dormant]` / `[/雾化]` etc.

Click a row to see full content in the Detail tab.

### 4.2 Detail

Shows full information for the selected segment:

- Topic + keywords + anchor sentence
- Full conversation text
- Related segments (supersedes / superseded by / related_to)

### 4.3 Knowledge Graph

Visualizes relationships between segments as a network. Node size = importance, edge color = relationship type.

### 4.4 Actions

Four actions on the currently-selected segment:

| Button | Behavior |
|---|---|
| `/重要` | Set current segment's score → 10000, locked in L1 |
| `/循环 X` | Add a cycle tag (e.g. "Monday"), skip time decay |
| `/归档` | Set tier → L3_hidden, no proactive recall |
| `/雾化` | **Physically erase L0 detail** (irreversible!) |

> ⚠️ **`/雾化` is irreversible!** A strong-warning dialog appears; you must type `我确认雾化` to confirm. After fogging, the original conversation text is permanently deleted — only the topic title, keywords, and an anchor sentence (≤ 20 chars) remain.  
> See [Section 7](#7-urgent-item-tracking-紧急--完成--延期) for detailed fogging rules.

### 4.5 4 Quadrants

Classifies segments by "important + urgent" — the most intuitive "assistant's brain" view. Covered in detail in Section 6.

---

## 5. Day-to-day use: the CLI

Not a GUI fan? MTCA also provides a command line.

### 5.1 Timeline CLI

```bash
# Today's timeline
python -m src.cli.timeline timeline

# Last 7 days
python -m src.cli.timeline timeline --period week

# Filter by project
python -m src.cli.timeline timeline --project "MTCA"

# Keyword search
python -m src.cli.timeline timeline --query "demo"

# Tree view
python -m src.cli.timeline tree --project "MTCA"
```

Output looks like this (terminal-style black-and-white):

![CLI timeline output](docs/assets/user_guide/en_cli_timeline.png)

Each line: time range + topic + score + state.

### 5.2 User controls CLI

7 commands — usable in conversation or terminal:

```bash
python -m src.cli.user_controls --help
```

![CLI user controls](docs/assets/user_guide/en_cli_help.png)

Command reference:

| Command | What it does | Reversible? |
|---|---|---|
| `important <id>` | `/重要`: lock score, stay in L1 forever | ✅ can downgrade |
| `cycle <id> <tag>` | `/循环`: add cycle tag, skip time decay | ✅ can cancel |
| `archive <id>` | `/归档`: tier → L3_hidden, no proactive recall | ✅ can restore |
| `fog <id> --anchor "..."` | `/雾化`: physically erase detail | ❌ **irreversible** |
| `urgent <id> <when>` | `/紧急 <seg> <deadline>`: mark segment as urgent-tracked | ✅ |
| `done <id>` | `/完成`: mark expired/tracking segment as completed | ✅ |
| `postpone <id> [when]` | `/延期 <seg> [new-time]`: reset expiry (default +7d) | ✅ |

Time formats: tomorrow / day-after / next-week / 3d / 7d / `2026-07-10` / millisecond timestamp.

### 5.3 Full example

Mark "tomorrow's client demo PPT not done yet" as urgent, must reply before 14:00:

```bash
# 1. Find the segment ID (in timeline output)
python -m src.cli.timeline timeline --query "PPT"

# 2. Suppose returned ID is seg-xxxxxx
python -m src.cli.user_controls urgent seg-xxxxxx "tomorrow" --anchor "PPT must be done before demo"

# 3. Output: [OK] 已 /紧急 seg-xxxxxx → expires=... (state=tracking)
```

![CLI /urgent command demo](docs/assets/user_guide/en_cli_urgent.png)

Once done, the segment appears in the GUI "4 象限" tab's URGENT bar at the top, and you'll be reminded when the time comes.

---

## 6. Understanding the 4 quadrants

MTCA classifies all segments by "important + urgent" — the most intuitive "assistant's brain" view.

### 6.1 Two dimensions

| Dimension | Meaning | Range |
|---|---|---|
| **urgency** | Time pressure: high = rush / low = relaxed | 0.0 – 1.0 |
| **importance** | Long-term value: high = important / low = chitchat | 0.0 – 1.0 |

### 6.2 Four quadrants + 1 special class

| Class | Importance | Urgency | Half-life | Examples |
|---|---|---|---|---|
| **Q1 important + urgent** | ≥ 0.7 | ≥ 0.7 | 180 days | "Tomorrow's client demo", "Monthly report due" |
| **Q2 important + not urgent** | ≥ 0.7 | < 0.7 | 365 days | "Reading notes", "OKRs" |
| **Q3 not important + urgent** | < 0.7 | ≥ 0.7 | 14 days | "Buy eggs and milk", "Turn on AC at 3 PM" |
| **Q4 neither** | < 0.7 | < 0.7 | 3 days | "Nice weather today", "Coffee machine got replaced" |
| **URGENT (deadline-emphasized)** | any | any | **never decays** | "Must reply to client email by 17:00 today", "Due Monday" |

**Key rules**:

- **Q1 segments + `/重要`-marked segments + URGENT segments = 3 classes that never forget** (Iron Rule #9)
- Other quadrants' segments gradually "sink" over time; keyword search still finds them, but they won't proactively surface

### 6.3 URGENT bar: overdue urgent items

When a URGENT segment expires (the deadline you set has passed), it disappears from its original quadrant and pops up in the URGENT bar at top:

> ⚠ N overdue urgent items pending (double-click to view)

Now you need to decide:

- **Done**: run `done <id>`
- **Need postponement**: run `postpone <id> 7d`
- **Actually important**: run `important <id>` to lock it

### 6.4 Quadrant color rules

| Quadrant | Color | Meaning |
|---|---|---|
| Q1 | 🔴 Red | Important + urgent — must do today / this week |
| Q2 | 🟢 Cyan | Important + not urgent — long-term value |
| Q3 | 🟠 Orange | Not important + urgent — chores |
| Q4 | ⚫ Grey | Chitchat / mood — disposable |

URGENT tracking segments get a `⚠` prefix in their quadrant; expired segments are bolded.

---

## 7. Urgent item tracking (`/紧急` `/完成` `/延期`)

This chapter covers MTCA's most "assistant-like" feature: **urgent item tracking**.

### 7.1 What problem it solves

You often have this anxiety: "how many things do I actually have to do this month? Which ones are overdue?"

MTCA's urgent tracking gives you a **checklist + auto-reminder**. Once you mark `/紧急`, the system:

1. Puts the segment in Q1 of the 4 quadrants (if it's already urgent) or creates a new URGENT tracking segment
2. The URGENT bar at the top keeps showing remaining / overdue status
3. When time's up, it pops from its original location to the URGENT bar to remind you "you need to handle this"

### 7.2 Four scenario examples

#### Scenario A: Monthly report due tomorrow

```bash
# Find the report segment ID
python -m src.cli.timeline timeline --query "report"

# Mark urgent, deadline tomorrow
python -m src.cli.user_controls urgent seg-report-id "tomorrow" --anchor "monthly report due"
```

When tomorrow comes, this segment auto-appears in the URGENT bar.

#### Scenario B: Completed urgent items

```bash
# I already finished it
python -m src.cli.user_controls done seg-report-id
```

Status changes to `completed`, disappears from both the bar and the 4 quadrants.

#### Scenario C: Need to postpone

```bash
# Push 7 days forward
python -m src.cli.user_controls postpone seg-report-id "7d"

# Or push to next Friday
python -m src.cli.user_controls postpone seg-report-id "2026-07-15"
```

#### Scenario D: Realize this is important long-term

```bash
# Upgrade to /重要, never lose
python -m src.cli.user_controls important seg-report-id
```

### 7.3 Time format cheat sheet

Both `/紧急 <time>` and `/延期 <time>` support these formats:

| Format | Meaning |
|---|---|
| `明天` / `后天` / `tomorrow` / `day-after` | Relative 1 / 2 days |
| `下周` / `下月` / `next-week` / `next-month` | Relative 7 / 30 days |
| `3d` / `7d` / `30d` | N days later |
| `尽快` / `马上` / `立刻` / `asap` / `now` | Today |
| `2026-07-10` | ISO date |
| `2026-07-10T15:30` | ISO date + time |
| `1710000000000` | Millisecond timestamp |

> Note: Chinese keywords (`明天`, `后天`, etc.) work in any locale. English equivalents (`tomorrow`, `day-after`, `asap`) are listed for reference but the parser primarily recognizes Chinese tokens — use ISO date or `Nd` for cross-language compatibility.

---

## 8. Back up your data

MTCA's data lives in **a single SQLite file on your local computer**. If that file is lost, everything is gone — so backups matter.

### 8.1 Where the database lives

| Platform | Path |
|---|---|
| **Windows** | `C:\Users\<your-username>\.mtca\mtca.db` |
| **macOS / Linux** | `~/.mtca/mtca.db` |

An auto-generated `backups/` directory sits next to the DB, with one backup per day.

### 8.2 Automatic backup

On startup, MTCA automatically:

1. Copies the current `mtca.db` to `backups/mtca-YYYYMMDD.db`
2. Cleans up backups older than 7 days

No manual work needed. To view backups:

- Windows Explorer: navigate to `C:\Users\<you>\.mtca\backups\`
- Terminal: `ls ~/.mtca/backups/`

### 8.3 Manual backup

To be extra safe, manually copy once in a while:

**Windows**:

```powershell
# Copy whole directory to OneDrive
Copy-Item -Recurse $env:USERPROFILE\.mtca $env:USERPROFILE\OneDrive\mtca-backup-2026-07-06
```

**macOS / Linux**:

```bash
cp -r ~/.mtca ~/iCloud/mtca-backup-$(date +%Y-%m-%d)
```

### 8.4 Cross-device sync

MTCA's SQLite is a single file — you can:

- **Cloud sync**: put the entire `~/.mtca/` directory under OneDrive / iCloud / Syncthing / Dropbox  
  ⚠️ **Don't open the same DB on two machines simultaneously** (SQLite file locking). Close MTCA before syncing.
- **Manual USB drive**: copy the DB file to another machine's `~/.mtca/` directory weekly / monthly

---

## 9. FAQ

### Q1: GUI opens empty after install — is that normal?

Yes. If you just installed and haven't connected any AI tool to MTCA yet, the database is naturally empty. Empty timeline and 4 quadrants are expected behavior.

### Q2: How do I automatically write AI conversations to MTCA?

MTCA doesn't directly "hook into" AI tools. The options are:

- **MCP Server (recommended)**: let Claude Code / Cursor connect with a 5-line config. See [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md)
- **Manual CLI writes**: use `python -m src.cli.user_controls` series commands

### Q3: 4-quadrant tab errors out — what do I do?

The most common cause is that **the database hasn't gone through the M2.5.1 schema migration**. Open a Python terminal and run:

```python
from src.store.sqlite import init_db
init_db()
```

This triggers the schema migration, adding 5 new columns to the segments table (urgency / importance / emotion / expires / urgent_state). Restart GUI afterwards and it'll work.

### Q4: Can I undo `/雾化`?

**No.** Fogging **physically erases** the original conversation text — only the skeleton remains (topic title + time + keywords + anchor sentence ≤ 20 chars). On recall, the first time AI returns "segment deleted, keywords: xxx + anchor sentence"; the second time, status switches to `archived` and stops being proactively returned.

**Rule of thumb**: mark `/重要` *before* fogging. The skeleton (title + keywords) is always preserved, so AI at least remembers "you did this thing".

### Q5: Can multiple people share one database?

No. MTCA is a single-user local tool; SQLite file locking doesn't support concurrent writers. If you want multi-device:

- Each person has their own `~/.mtca/` directory
- Use cloud drive for **backup-level** sync (not real-time) — close MTCA before syncing

### Q6: How much disk space does it use?

**About 1 – 2 GB / year** of plain text. If you chat heavily (1000+ messages / day), expect 5 – 8 GB / year.

To free up: delete old files in `~/.mtca/backups/`.

### Q7: How do I see which "important" content is about to expire?

The URGENT bar at the top of the GUI's 4-quadrant tab always shows this. From CLI:

```bash
python -m src.cli.timeline timeline --query "important"
```

You can also search by the `/重要` tag in the timeline.

### Q8: Where's my LLM provider config?

`~/.mtca/config.toml` — auto-generated on first launch. Supported providers:

- `ollama` (local, recommended)
- `lmstudio`
- `llamacpp` (server mode)
- `cloud` (needs API key)

Full configuration: [docs/QUICKSTART.md §6](QUICKSTART.md).

### Q9: Does MTCA conflict with my ChatGPT / Claude?

**No, not at all.** MTCA is a sidecar tool — it doesn't take over any AI platform. Your ChatGPT / Claude work as usual; MTCA silently records conversations in the background.

> Design principle: **Agent runs its course, MTCA runs its course, no interference, recall on demand.**

### Q10: Can I use pure command line, no GUI?

Yes. All core functions (timeline query / segment operations / urgent tracking) have CLI commands. The GUI is just a more intuitive visual wrapper.

---

## 10. Cheat sheet

| I want to… | How |
|---|---|
| Open GUI | `python gui/run.py` |
| Today's timeline | `python -m src.cli.timeline timeline` |
| Last 7 days | `python -m src.cli.timeline timeline --period week` |
| Last 30 days | `python -m src.cli.timeline timeline --period month` |
| Keyword search | `python -m src.cli.timeline timeline --query "keyword"` |
| Filter by project | `python -m src.cli.timeline timeline --project "project-name"` |
| Mark important | `python -m src.cli.user_controls important <seg_id>` |
| Mark urgent + deadline | `python -m src.cli.user_controls urgent <seg_id> "tomorrow" --anchor "note"` |
| Mark done | `python -m src.cli.user_controls done <seg_id>` |
| Postpone | `python -m src.cli.user_controls postpone <seg_id> "7d"` |
| Archive | `python -m src.cli.user_controls archive <seg_id>` |
| Fog (irreversible!) | `python -m src.cli.user_controls fog <seg_id> --anchor "≤20 char anchor"` |
| Database location | `~/.mtca/mtca.db` (Windows: `%USERPROFILE%\.mtca\mtca.db`) |
| Backup directory | `~/.mtca/backups/` |

---

## 11. Advanced usage

Once you're comfortable, here's where to go deeper:

| You want to… | See |
|---|---|
| Developer cheat sheet (pip install / API / data structure) | [docs/QUICKSTART.md](QUICKSTART.md) |
| Full `/重要 /循环 /归档 /雾化` spec | [docs/USER_CONTROLS.md](USER_CONTROLS.md) |
| Connect Claude Code / Cursor via MCP | [docs/MCP_INTEGRATION.md](MCP_INTEGRATION.md) |
| Overall architecture + 8+1 iron rules | [docs/ARCHITECTURE.md](ARCHITECTURE.md) |
| L0 / L1 / L2 / L3 data model | [docs/DATA_MODEL.md](DATA_MODEL.md) |
| Pick an LLM provider / configuration | [docs/LLM_PROVIDERS.md](LLM_PROVIDERS.md) |

**Nine iron rules (MTCA's non-negotiable bottom line)**:

1. User owns the data (local SQLite)
2. L0 dual-layer + AI-immutable (skeleton / detail split; AI can't modify)
3. L1 – L3 are views (rebuildable, lossy)
4. Fogging = user-initiated detail disposal (irreversible)
5. Recall errors auto-fall back to L0 skeleton
6. User control commands reach L0 directly (`/重要 /循环 /归档 /雾化`)
7. GUI-first (CLI is development-only aid)
8. Storage cost borne by user (1 – 2 GB / year)
9. **Important memories never forgotten** (Q1 locked / URGENT non-decaying / `/重要` frozen)

---

**Feedback**: rule disagreement → edit `AI_RULES.md`; doc errors → edit this file or open GitHub Issue; bugs → open GitHub Issue.
