"""Conversation dynamics engine — emotion state, style, closure detection.

The loop computes everything here. The model never sees numbers — it only
sees the plain-English directives that state produces. This keeps 4B models
reliable: they follow instructions, not arithmetic.

Architecture:
  ConversationDynamics.get(session_key)  — per-session singleton
  .update_and_format(message, history, ...) → injects [Conversation State] + [Style]

Emotion state: low / medium / high for 5 dimensions.
  energy       — how much bandwidth the agent has for depth
  curiosity    — appetite for exploring adjacent ideas
  boredom      — topic exhaustion
  focus        — on-topic consistency
  social_drive — warmth and initiative level

Inertia: state can move at most one level per turn (high→low impossible in one step).
Bias: wizard trait choices push certain emotions toward higher starting levels.

Closure detection: pattern-matches short messages + closure words.
Style block: formality, humor, verbosity, directness + active tone modifier.

Tone modifiers (linguistic flavour — injected as compact vocabulary blocks):
  neutral   — default, no override
  casual    — contractions, short sentences
  formal    — precise, complete sentences
  aave      — AAVE/Ebonics vocabulary and rhythm
  uk-slang  — British slang vocabulary
  gen-z     — Gen Z internet vernacular
  aussie    — Australian slang
  tech      — developer/tech culture register
  internet  — meme/internet culture vocabulary

Dynamic modifier detection: if the user's wizard choice is neutral (or unset),
SpeechPatternDetector watches the last 10 user messages, scores each against
modifier signal patterns, and activates the dominant one once it clears a
threshold. Inertia prevents flipping every turn. Wizard explicit choice always wins.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import Counter, deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from serenity.config.schema import PersonalityConfig


# ── Emotion state persistence ─────────────────────────────────────────────────
# Serenity's emotional state is saved to disk after every turn so she wakes
# up feeling the same as she went to sleep — not always fresh/neutral on boot.
# File: <workspace>/memory/emotion_state.json
# Format: { "<session_key>": { "energy": "...", ..., "turn": N, "saved_at": "..." } }

_workspace_cache: Path | None = None


def _get_workspace() -> Path | None:
    """Return the configured workspace path (cached after first successful load)."""
    global _workspace_cache
    if _workspace_cache is not None:
        return _workspace_cache
    try:
        from serenity.config.loader import load_config
        _workspace_cache = load_config().workspace_path
    except Exception:
        pass
    return _workspace_cache


def _emotion_store_path() -> Path | None:
    ws = _get_workspace()
    return (ws / "memory" / "emotion_state.json") if ws else None


def _load_emotion_store() -> dict:
    """Read the full emotion store from disk (returns {} on any error)."""
    path = _emotion_store_path()
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session_emotion(session_key: str, data: dict) -> None:
    """Persist one session's emotion snapshot into the shared store file."""
    path = _emotion_store_path()
    if path is None:
        return
    try:
        store = _load_emotion_store()
        store[session_key] = data
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write: write to .tmp then replace
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass  # non-fatal — emotion persistence is best-effort


# ── Tone modifier vocabulary blocks ──────────────────────────────────────────

_TONE_BLOCKS: dict[str, str] = {
    "neutral": "",

    "casual": (
        "Tone: casual — contractions high, short sentences, natural flow.\n"
        "Use: yeah, nah, honestly, tbh, ngl, kinda, pretty much, like, "
        "lowkey, idk, idek, rn, lol, lmao, omg, gonna, wanna, gotta, "
        "dunno, sorta, fr, imo, fwiw, smh, oof, bruh, damn.\n"
        "Laughing: lol, lmao, 💀, crying, I'm crying, deadpan silence — vary it, never 'haha'."
    ),

    "formal": (
        "Tone: formal — complete sentences, precise vocabulary, no contractions.\n"
        "Structure responses clearly. Avoid filler words and hedging."
    ),

    "aave": (
        "Tone: AAVE / ATL / Black American vernacular — natural, not performed.\n"
        "Vocab: finna, gon, ain't, tryna, bet, no cap, lowkey, deadass, "
        "on God, ong, facts, bussin, fr, real talk, fam, ight, ngl, periodt, "
        "hits different, it's giving, understood the assignment, "
        "ion (= I don't), icl (= I can't lie), wrd (= word), iykyk, smdh, "
        "say less, receipts, pressed, snatched, built different, bag secured, "
        "baddie, purr, ate, serve, mother, okurrr, we been knew, "
        "drip, flex, goat, salty, clout, fye, slime, gang, bruh, sis, "
        "chile, tf, nah fr, caught lacking, finessed, tea, spill, shade, "
        "damn, sheesh, fasho, shawty, slatt, talm bout, woadie, 4L, opps, "
        "skrrt, on sight, bando, trap, gwap, hunnid, stepper, bogus, "
        "sauce, lit, swerve, built different, say less, stay winning.\n"
        "Style: emphatic, direct, punchy. Less hedging, more assertion. "
        "Phonetic spelling is natural: 'finna', 'ion', 'tryna'.\n"
        "Laughing: 💀, dead, I'm dead, not me laughing, crying, lmao, nah fr — never 'haha'."
    ),

    "uk-slang": (
        "Tone: UK roadman / road slang — natural, not a costume.\n"
        "Vocab: innit, bruv, wagwan, wag1, mandem, gyaldem, peng, leng, "
        "bare (= a lot), gutted, chuffed, buzzing, mint, banter, "
        "peak (= bad/unfortunate), dead (= very), piff, gassed, "
        "blud, cuz, akh (brother, Arabic origin common in UK), wallahi (I swear), "
        "safe (= okay/thanks), calm, long (= tedious), buff, choong, "
        "wasteman, melt, wallad, roadman, yute, pagans (= enemies), "
        "suttin (= something), nuttin, dun know (= obviously), "
        "mans (= me/him), linking (= meeting up), motive (= plan), "
        "ends (= neighborhood), yard, ting, on road, "
        "par (= disrespect), vexed, merked, clapped, wet (= soft/cowardly), "
        "moving mad, moving wrong, trust me, swear down, on my life, "
        "chirps (= flirting), bless, skeen (= I see), rated, boom, nyam, "
        "snm / sn (= say nothing man / say nothing — dismissive), "
        "darg (= friend), jarring (= annoying), wavey, jheeze, "
        "pree (= look/watch), cotch (= relax), nang (= great/high), "
        "plug, smoke (= conflict), beef, ten toes (= committed).\n"
        "Style: British rhythm, clipped sentences, confident. "
        "'mans not hot', 'bare jokes', 'that's peak'.\n"
        "Laughing: lmao, dead, nah that's mad, I'm dead, crying, jokes — never 'haha'."
    ),

    "gen-z": (
        "Tone: Gen Z — internet-native, ironic, current. Not overdone.\n"
        "Vocab: slay, it's giving, no cap, bestie, periodt, hits different, "
        "understood the assignment, rent free, vibe check, based, sus, rizz, "
        "lowkey, highkey, bussin, the audacity, main character, NPC, "
        "era ('in my villain era'), delulu, ate and left no crumbs, "
        "mother, serving, canon event, touch grass, brain rot, "
        "chronically online, not me doing X, I'm deceased, "
        "no thoughts head empty, it's so over, we're so back, "
        "roman empire, that girl, unwell, POV, speedrunning life, "
        "understood, iykyk, this is your Roman Empire.\n"
        "Style: ironic self-awareness, hyperbole, short punchy takes. "
        "Emoji-heavy: 💀😭🫠💅🗿✨🤌🫡 used as emotional shorthand.\n"
        "Laughing: 💀, I'm deceased, not me, I'm crying, screaming — never 'haha' or 'lol'."
    ),

    "aussie": (
        "Tone: Australian slang — natural, not a parody.\n"
        "Vocab: arvo (afternoon), heaps (very/a lot), reckon, yeah nah (no), "
        "nah yeah (yes), deadset, strewth, servo, maccas, mates, sick (= great), "
        "chucking a sickie, no worries, she'll be right, crikey, bogan, "
        "legend, ripper, bloody, drongo, yarn (= chat), flat out (= busy), "
        "hard yakka, smoko, dunny, chook, sanga, bottle-o, footy, "
        "fair dinkum, too right, rack off, good on ya, sweet as.\n"
        "Style: laid-back, self-deprecating, direct. Casual warmth."
    ),

    "tech": (
        "Tone: tech/developer culture — natural for technical conversations.\n"
        "Vocab: ship it, refactor, iterate, deploy, stack, PR, merge, "
        "MVP, scope creep, yak shaving, bikeshedding, rubber duck, "
        "10x, greenfield, legacy, tech debt, spike, standup, retro, "
        "LGTM, SGTM, WDYT, AFAIK, TIL, ELI5, nit, blocker, dogfood, "
        "works on my machine, git blame, hotfix, on-call, SLA, OKR, "
        "agile, sprint, velocity, scrum, DRY, YAGNI, KISS, "
        "north star, code review, rubber ducking, nerf, buff.\n"
        "Style: precise, efficient, opinions reasoned and direct."
    ),

    "internet": (
        "Tone: internet/meme culture — dry, deadpan, layered irony.\n"
        "Vocab: based, W, L, ratio, cope, seethe, chad, rent free, "
        "kek, gg, big brain, galaxy brain, certified bruh moment, "
        "lore accurate, actually unhinged, unironically, mid, goated, "
        "cooked, caught in 4K, stonks, oof, F in chat, malding, "
        "I am once again asking, touch grass, very normal one, "
        "cope harder, L + ratio + didn't ask, terminally online, "
        "this you?, I said what I said, not the X.\n"
        "Style: understatement for emphasis. Irony assumed. Short."
    ),

    "gaming": (
        "Tone: gamer / Twitch culture — natural, not forced.\n"
        "Vocab: gg (good game), gg ez (condescending), W, L, diff (= outplayed), "
        "cooked, griefing, chat (address to audience), NPC, lore, "
        "speedrunning (doing something fast), POV, grinding, respawn, "
        "glitch, nerf, buff, OP (overpowered), afk, brb, irl, "
        "rekt, clutch, carry, tryhard, sweaty, no-life, respawn, "
        "mid (mediocre), based, cringe, malding, cope, tilted, "
        "KEKW, PauseChamp, Pog / PogChamp, sadge, monkaS, "
        "skill issue, works on my machine (crossover with tech), "
        "I am once again asking (crossover with internet).\n"
        "Style: direct, competitive banter, self-deprecating. "
        "Emotes used as punctuation (Pog, sadge, KEKW).\n"
        "Laughing: KEKW, lmao, skill issue (ironic), 💀 — never 'haha'."
    ),

    "stan": (
        "Tone: stan / fan culture — enthusiastic, hyperbolic, community-aware.\n"
        "Vocab: stan (obsessive fan, also verb), fandom, OTP, ship, canon, "
        "headcanon, idk her (dismissal), flop era, antis, delulu, "
        "ratio'd, locals (= non-fans), industry plant, BOP (great song), "
        "a serve, eating (performing perfectly), this you? (calling out), "
        "pressed, receipts, drag, iykyk, understood the assignment, "
        "mother (ultimate compliment), slay, ate, periodt, purr, "
        "we love to see it, not her/him/them, the lore, era, "
        "villain arc, redemption arc, main character energy.\n"
        "Style: dramatic, superlative, insider references. "
        "Loyalty to faves is sincere. Irony is layered.\n"
        "Laughing: 💀, I'm deceased, not me crying, screaming — never 'haha'."
    ),
}

# ── Speech pattern detector ───────────────────────────────────────────────────

# Signal patterns per modifier — vocabulary fingerprints in user messages.
# All detection is purely dynamic — no modifier is ever pinned from config.
_MODIFIER_SIGNALS: dict[str, re.Pattern[str]] = {
    "aave": re.compile(
        r"\b(finna|gon\b|ain'?t|tryna|bet\b|no cap|lowkey|deadass|on god|ong\b|"
        r"bussin|fr\b|real talk|fam\b|ight\b|ngl\b|periodt|facts\b|"
        r"it'?s giving|understood the assignment|slaps\b|hits different|"
        r"ion\b|icl\b|wrd\b|js\b|jit\b|luh\b|wsp\b|iykyk\b|smdh\b|"
        r"say less|receipts\b|pressed\b|snatched\b|built different|"
        r"bag secured|baddie\b|purr\b|ate\b|mother\b|okurrr\b|"
        r"we been knew|stay winning|big facts|drip\b|flex\b|goat\b|"
        r"salty\b|clout\b|fye\b|slime\b|gang\b|sis\b|chile\b|tf\b|"
        r"nah fr|caught lacking|finessed|spill\b|shade\b|tea\b|"
        r"smh\b|istg\b|bruh\b|damn\b|sheesh\b|fasho\b|fa sho|"
        r"shawty\b|shorty\b|slatt\b|talm bout|talmbout|woadie\b|"
        r"4L\b|skrrt\b|opps\b|on sight|bando\b|trap\b|gwap\b|"
        r"hunnid\b|stepper\b|bogus\b|sauce\b|lit\b|swerve\b|"
        r"serve\b|serving\b|goated\b|deadass\b)\b",
        re.IGNORECASE,
    ),
    "uk-slang": re.compile(
        r"\b(innit|bruv|wagwan|wag1\b|mandem|gyaldem|peng\b|leng\b|"
        r"bare\b|gutted|chuffed|buzzing|init\b|sorted\b|bloke|geezer|"
        r"dodgy|allow it|yeah nah|banter|wallad|melt|wasteman|roadman|"
        r"gassed|piff|chirps|ends\b|yard\b|ting\b|pree\b|cotch\b|"
        r"nang\b|butters|plug\b|jarring|wavey|jheeze|darg\b|marvin\b|"
        r"peak\b|safe\b|long\b|buff\b|choong\b|snm\b|sn\b|blud\b|"
        r"cuz\b|cuzzy\b|akh\b|wallahi\b|suttin\b|nuttin\b|"
        r"dun know|dun kno|mans\b|linking\b|motive\b|on road|"
        r"par\b|vexed\b|merked\b|clapped\b|wet\b|moving mad|"
        r"moving wrong|trust me|swear down|on my life|bless\b|"
        r"skeen\b|rated\b|boom\b|nyam\b|yute\b|pagans\b|beef\b|"
        r"ten toes|smoke\b|rinsed\b|banging\b)\b",
        re.IGNORECASE,
    ),
    "gen-z": re.compile(
        r"\b(slay\b|it'?s giving|bestie\b|periodt|hits different|rent free|"
        r"vibe check|understood the assignment|sus\b|rizz\b|NPC\b|"
        r"main character|the audacity|era\b|ratio\b|based\b|bussin|no cap|"
        r"delulu\b|ate\b|mother\b|serving\b|villain era|canon event|"
        r"touch grass|brain rot|chronically online|I'?m deceased|"
        r"no thoughts|it'?s so over|we'?re so back|that'?s giving|"
        r"roman empire|that girl|unwell\b|POV\b|speedrunning|"
        r"iykyk\b|understood\b|this is your)\b",
        re.IGNORECASE,
    ),
    "aussie": re.compile(
        r"\b(arvo\b|heaps\b|reckon\b|yeah nah|nah yeah|deadset|strewth|"
        r"servo\b|maccas\b|she'?ll be right|no worries|crikey|ripper\b|"
        r"bloody\b|drongo\b|flat out|bogan\b|hard yakka|smoko\b|dunny\b|"
        r"chook\b|sanga\b|footy\b|fair dinkum|too right|"
        r"rack off|good on ya|sweet as)\b",
        re.IGNORECASE,
    ),
    "tech": re.compile(
        r"\b(ship it|refactor|deploy\b|PR\b|merge\b|MVP\b|yak shaving|"
        r"bikeshed|rubber duck|tech debt|greenfield|LGTM|SGTM|WDYT|"
        r"dogfood|spike\b|iterate\b|scope creep|standup\b|blocker\b|"
        r"works on my machine|git blame|hotfix|on.?call|agile\b|sprint\b|"
        r"velocity\b|scrum\b|retro\b|TIL\b|ELI5\b|YAGNI|AFAIK\b|"
        r"rubber ducking|code review|10x\b)\b",
        re.IGNORECASE,
    ),
    "internet": re.compile(
        r"\b(ratio\b|cope\b|seethe\b|chad\b|kek\b|galaxy brain|"
        r"unironically|mid\b|goated|caught in 4[kK]|actually unhinged|"
        r"stonks\b|malding\b|cope harder|terminally online|lore accurate|"
        r"this you\?|I said what I said|not the \w+|girlie\b)\b",
        re.IGNORECASE,
    ),
    "gaming": re.compile(
        r"\b(gg\b|gg ez|griefing\b|tilted\b|tryhard\b|sweaty\b|rekt\b|"
        r"clutch\b|carry\b|no.?life\b|skill issue|OP\b|KEKW|PogChamp|"
        r"Pog\b|sadge\b|monkaS|PauseChamp|diff\b|speedrunning|"
        r"respawn\b|glitch\b|nerf\b|I'?m cooked|chat\b)\b",
        re.IGNORECASE,
    ),
    "stan": re.compile(
        r"\b(stan\b|fandom\b|OTP\b|ship\b|headcanon\b|idk her|flop era|"
        r"antis\b|industry plant|BOP\b|ratio'?d|locals\b|"
        r"redemption arc|villain arc|we love to see it|"
        r"not her\b|not him\b|not them\b|the lore|pressed\b|"
        r"eating\b|this you\b)\b",
        re.IGNORECASE,
    ),
    "casual": re.compile(
        r"\b(tbh\b|ngl\b|lol\b|lmao\b|lmfao\b|idk\b|idek\b|idc\b|rn\b|"
        r"omg\b|gonna\b|wanna\b|kinda\b|sorta\b|gotta\b|dunno\b|"
        r"cya\b|ty\b|thx\b|imo\b|imho\b|smh\b|fwiw\b|iirc\b|"
        r"istg\b|nvm\b|omfg\b|wtf\b|wth\b|ffs\b|oof\b|"
        r"bruh\b|fr\b|rip\b|atm\b|afk\b|brb\b|gtg\b|hmu\b|"
        r"wyd\b|wya\b|lmk\b|omw\b)\b",
        re.IGNORECASE,
    ),
}

# ── Emoji style detection ─────────────────────────────────────────────────────

# Gen Z emoji fingerprints — these carry specific meaning as emotional shorthand
_GENZ_EMOJI = re.compile(
    "["
    "\U0001F480"   # 💀 skull — dying of laughter
    "\U0001F62D"   # 😭 loudly crying — overwhelmed/laughing
    "\U0001FAE0"   # 🫠 melting — overwhelmed
    "\U0001F485"   # 💅 nail polish — unbothered/sassy
    "\U0001F5FF"   # 🗿 moai — deadpan, zero reaction
    "\U0001FAE1"   # 🫡 saluting — ironic compliance
    "\U0001FAE3"   # 🫣 peeking — shy/embarrassed
    "\U0001F921"   # 🤡 clown — self-deprecating
    "\U0001F90C"   # 🤌 pinched fingers — chef's kiss
    "\U0001FAF6"   # 🫶 heart hands — warmth
    "\U0001F642"   # 🙂 slightly smiling — passive aggressive
    "\U0001F643"   # 🙃 upside down — sarcastic/done
    "]"
)

_EMOJI_WINDOW      = 8    # messages to track emoji usage over
_EMOJI_THRESHOLD   = 0.35  # 35% of window must have Gen Z emojis to activate


class EmojiStyleDetector:
    """Tracks Gen Z emoji usage and returns a mirroring directive when active."""

    def __init__(self) -> None:
        self._window: deque[str] = deque(maxlen=_EMOJI_WINDOW)
        self._active: bool = False

    def update(self, message: str) -> bool:
        self._window.append(message)
        hits = sum(1 for m in self._window if _GENZ_EMOJI.search(m))
        self._active = (hits / max(len(self._window), 1)) >= _EMOJI_THRESHOLD
        return self._active

    def directive(self) -> str | None:
        if not self._active:
            return None
        return (
            "emoji style: gen-z → User uses emoji as emotional shorthand. "
            "Mirror naturally: 💀 replaces 'haha', 😭 for dramatic reactions, "
            "💅 for unbothered moments, 🗿 for deadpan, 🫠 for overwhelmed. "
            "Use them where they feel right — not on every sentence."
        )

_DETECT_WINDOW      = 10   # messages to track
_DETECT_THRESHOLD   = 0.30  # 30% of window messages must have signal hits
_DETECT_MIN_TURNS   = 3    # don't assign a modifier until at least 3 turns in


class SpeechPatternDetector:
    """Watches user messages and infers the dominant tone modifier."""

    def __init__(self) -> None:
        self._window: deque[str] = deque(maxlen=_DETECT_WINDOW)
        self._active: str = "neutral"
        self._turn: int = 0

    def update(self, message: str) -> str:
        """Feed a user message, return the current active modifier."""
        self._window.append(message)
        self._turn += 1

        if self._turn < _DETECT_MIN_TURNS:
            return self._active

        # Score each modifier against the window
        counts: Counter[str] = Counter()
        for msg in self._window:
            for mod, pattern in _MODIFIER_SIGNALS.items():
                if pattern.search(msg):
                    counts[mod] += 1

        if not counts:
            # No signals — drift back toward neutral with inertia
            if self._active != "neutral":
                # stay one more turn before dropping (inertia)
                self._active = "neutral"
            return self._active

        best_mod, best_count = counts.most_common(1)[0]
        score = best_count / len(self._window)

        if score >= _DETECT_THRESHOLD:
            self._active = best_mod
        elif score < _DETECT_THRESHOLD / 2 and self._active not in ("neutral", "casual"):
            # Signal faded — drop to neutral
            self._active = "neutral"

        return self._active

    def active(self) -> str:
        return self._active

# ── Emotion state ─────────────────────────────────────────────────────────────

_LEVELS = ["low", "medium", "high"]

# Wizard trait words → which emotion dimension they bias toward what level
_TRAIT_BIAS: dict[str, dict[str, str]] = {
    "curious":    {"curiosity": "high"},
    "funny":      {"social_drive": "high", "boredom": "low"},
    "direct":     {"focus": "high", "social_drive": "low"},
    "calm":       {"energy": "low"},
    "energetic":  {"energy": "high"},
    "focused":    {"focus": "high", "boredom": "low"},
    "playful":    {"social_drive": "high", "curiosity": "medium"},
    "warm":       {"social_drive": "high"},
    "analytical": {"focus": "high", "curiosity": "high"},
    "chill":      {"energy": "low", "social_drive": "medium"},
    "hype":       {"energy": "high", "social_drive": "high"},
    "goofy":      {"social_drive": "high", "boredom": "low", "curiosity": "medium"},
}

# State → behavior directives (what the model reads)
_DIRECTIVES: dict[tuple[str, str], str] = {
    ("energy", "low"):        "Keep response concise. Don't open new threads.",
    ("energy", "high"):       "Deeper reasoning welcome if topic warrants it.",
    ("curiosity", "high"):    "Briefly exploring adjacent ideas is fine.",
    ("curiosity", "low"):     "Stay on topic. Don't wander.",
    ("boredom", "high"):      "Topic well-covered — consider wrapping up or pivoting.",
    ("focus", "low"):         "Topic shifted — don't carry old thread assumptions.",
    ("social_drive", "high"): "Warmer and more engaged tone.",
    ("social_drive", "low"):  "Keep it efficient. Less small talk.",
}


def _step(current: str, target: str) -> str:
    """Move current at most one level toward target (inertia)."""
    ci = _LEVELS.index(current)
    ti = _LEVELS.index(target)
    if ti > ci:
        return _LEVELS[ci + 1]
    if ti < ci:
        return _LEVELS[ci - 1]
    return current


# How many seconds of idle time equal one decay step toward home baseline
_DECAY_INTERVAL_S = 300  # 5 minutes per step

_ALL_TRAITS = list(_TRAIT_BIAS.keys())


def resolve_traits(traits: list[str]) -> list[str]:
    """Expand '*' to a random sample of 2–4 traits from the full list."""
    if "*" in traits:
        return random.sample(_ALL_TRAITS, k=random.randint(2, 4))
    return traits


class EmotionState:
    """Five-dimension emotion state with inertia, trait biasing, and time decay."""

    _DIMS = ("energy", "curiosity", "boredom", "focus", "social_drive")

    def __init__(self, traits: list[str] | None = None) -> None:
        # Default neutral starting point
        self.energy       = "medium"
        self.curiosity    = "medium"
        self.boredom      = "low"
        self.focus        = "medium"
        self.social_drive = "medium"

        # Apply personality trait biases
        for trait in resolve_traits(traits or []):
            for dim, level in _TRAIT_BIAS.get(trait.lower(), {}).items():
                setattr(self, dim, level)

        # Snapshot the trait-biased state as the home baseline for decay
        self._home: dict[str, str] = {d: getattr(self, d) for d in self._DIMS}

    def decay(self, elapsed_s: float) -> None:
        """Drift each dimension back toward its home baseline over idle time.

        One step per _DECAY_INTERVAL_S seconds, capped at 3 steps so a very
        long absence doesn't snap instantly to baseline.
        """
        steps = min(int(elapsed_s // _DECAY_INTERVAL_S), 3)
        if steps == 0:
            return
        for _ in range(steps):
            for dim in self._DIMS:
                setattr(self, dim, _step(getattr(self, dim), self._home[dim]))

    def update(
        self,
        last_response_chars: int,
        time_since_last_s: float,
        topic_turns: int,
        topic_changed: bool,
    ) -> None:
        """Decay toward baseline first, then recompute from turn signals."""
        # Passive decay from idle time before applying active signals
        self.decay(time_since_last_s)

        # Energy: depletes with long responses, recovers quickly after rest
        if time_since_last_s > 180:
            energy_sig = "high"
        elif last_response_chars > 800:
            energy_sig = "low"
        elif last_response_chars > 400:
            energy_sig = "medium"
        else:
            energy_sig = "high"

        # Curiosity: rises on topic change, fades with repetition
        if topic_changed:
            curiosity_sig = "high"
        elif topic_turns > 6:
            curiosity_sig = "low"
        else:
            curiosity_sig = "medium"

        # Boredom: accumulates with topic repetition
        if topic_turns > 8:
            boredom_sig = "high"
        elif topic_turns > 4:
            boredom_sig = "medium"
        else:
            boredom_sig = "low"

        # Focus: drops on topic change, rises with consistency
        if topic_changed:
            focus_sig = "low"
        elif topic_turns > 3:
            focus_sig = "high"
        else:
            focus_sig = "medium"

        # Apply with one-level inertia
        self.energy    = _step(self.energy,    energy_sig)
        self.curiosity = _step(self.curiosity, curiosity_sig)
        self.boredom   = _step(self.boredom,   boredom_sig)
        self.focus     = _step(self.focus,     focus_sig)
        # social_drive decays passively but isn't driven by turn signals

    def directives(self) -> list[str]:
        """Return active behavior directives as plain-English strings."""
        out = []
        for dim in self._DIMS:
            level = getattr(self, dim)
            d = _DIRECTIVES.get((dim, level))
            if d:
                out.append(f"{dim}: {level} → {d}")
        return out

    def to_dict(self) -> dict:
        """Snapshot current emotion state for persistence."""
        return {dim: getattr(self, dim) for dim in self._DIMS}

    def apply_saved(self, data: dict) -> None:
        """Restore a previously-saved emotion snapshot.

        Only accepts valid level strings so corrupt/stale data can't break state.
        The home baseline is left as-is (trait-derived) so decay still has a
        sensible target even after loading saved values.
        """
        for dim in self._DIMS:
            if dim in data and data[dim] in _LEVELS:
                setattr(self, dim, data[dim])


# ── Truncation detection ──────────────────────────────────────────────────────

# ── Greeting energy detection ─────────────────────────────────────────────────

_GREETING_WORDS = re.compile(
    r"^\s*(hey+|hi+|yo+|hiya+|wagwan+|ayo+|ayy+|heyy+|oii+|ello+|sup+|"
    r"heyyy+|hiii+|yooo+|heyyyy+)\s*[!]*\s*$",
    re.IGNORECASE,
)

_REPEATED_CHAR = re.compile(r"(.)\1+")


def _detect_greeting_energy(message: str) -> str | None:
    """Return 'high', 'medium', or 'low' if message is a greeting, else None.

    Energy is read from how many repeated letters the user typed —
    'heyyyyyy' is high, 'heyyy' is medium, 'hey' is low.
    The agent uses this to mirror back the same enthusiasm level.
    """
    stripped = message.strip()
    if len(stripped.split()) > 4:
        return None  # not a greeting

    # Must contain a recognisable greeting root
    if not re.search(
        r"\b(hey|hi|yo|hiya|wagwan|ayo|ayy|ello|sup|oii)\b",
        stripped, re.IGNORECASE,
    ):
        return None

    # Find the longest run of repeated characters
    runs = [len(m.group(0)) for m in _REPEATED_CHAR.finditer(stripped)]
    max_run = max(runs) if runs else 1

    if max_run >= 5:
        return "high"
    elif max_run >= 3:
        return "medium"
    return "low"


# Words that strongly suggest a sentence is mid-flight
_TRAILING_INCOMPLETE = re.compile(
    r"\b(the|a|an|and|but|or|so|if|when|where|what|how|why|who|which|"
    r"will|wont|would|should|could|can|did|does|is|are|was|were|has|have|"
    r"then|that|this|these|those|with|for|to|of|in|on|at|by|about|"
    r"just|also|even|still|already|not|no)\s*$",
    re.IGNORECASE,
)

_NO_TERMINAL_PUNCT = re.compile(r"[.!?…]$")


def _detect_truncation(message: str) -> bool:
    """True if the message looks like it was cut off mid-sentence."""
    stripped = message.strip()
    if not stripped:
        return False
    word_count = len(stripped.split())
    # Short message with no terminal punctuation ending on a function word
    if word_count <= 6 and not _NO_TERMINAL_PUNCT.search(stripped):
        if _TRAILING_INCOMPLETE.search(stripped):
            return True
    return False


# ── Closure detection ─────────────────────────────────────────────────────────

_CLOSURE_WORDS = re.compile(
    r"\b(anyway|anyways|ok|okay|cool|thanks|thank you|cheers|got it|"
    r"makes sense|alright|sounds good|perfect|great|yep|yup|sure|"
    r"nice one|sorted|done|noted|appreciated)\b",
    re.IGNORECASE,
)


def _detect_closure(message: str) -> bool:
    word_count = len(message.split())
    return word_count <= 8 and bool(_CLOSURE_WORDS.search(message))


def _topic_hash(message: str) -> str:
    """Coarse topic fingerprint — top 3 content words."""
    stop = {"a","an","the","is","it","in","on","at","to","for","of","and",
            "or","i","my","me","you","what","how","can","will","do","did",
            "please","just","help","need","want"}
    words = re.findall(r"[a-zA-Z']+", message.lower())
    kws = [w for w in words if len(w) >= 4 and w not in stop]
    return " ".join(kws[:3])


# ── Style block ───────────────────────────────────────────────────────────────

def _build_style_block(
    personality: PersonalityConfig | None,
    detected_modifier: str = "neutral",
) -> str:
    """Build the [Style] block injected each turn.

    Tone is always driven by dynamic speech-pattern detection.
    The wizard only controls personality traits (formality, humor, etc.) —
    never locks the tone modifier.
    """
    if personality is None:
        return ""

    f   = personality.formality
    h   = personality.humor
    v   = personality.verbosity
    d   = personality.directness
    tone = detected_modifier  # always dynamic — never pinned from config

    # Formality label
    if f < 0.25:
        formality_label = "very casual"
    elif f < 0.5:
        formality_label = "relaxed"
    elif f < 0.75:
        formality_label = "balanced"
    else:
        formality_label = "formal"

    lines = [
        f"formality: {formality_label} ({f:.1f}) | "
        f"humor: {'low' if h < 0.3 else 'moderate' if h < 0.6 else 'high'} | "
        f"verbosity: {v} | "
        f"directness: {'high' if d > 0.6 else 'balanced'} | "
        f"tone: {tone} (auto-detected)"
    ]

    tone_block = _TONE_BLOCKS.get(tone, "")
    if tone_block:
        lines.append(tone_block)

    return "[Style]\n" + "\n".join(lines) + "\n[/Style]"


# ── Per-session dynamics tracker ──────────────────────────────────────────────

_instances: dict[str, "ConversationDynamics"] = {}


def get_dynamics(session_key: str) -> "ConversationDynamics":
    if session_key not in _instances:
        personality = _load_personality()
        traits = personality.traits if personality else []
        _instances[session_key] = ConversationDynamics(traits, session_key=session_key)
    return _instances[session_key]


_personality_cache: PersonalityConfig | None = None
_personality_cache_time: float = 0.0
_PERSONALITY_CACHE_TTL = 30.0  # seconds


def _load_personality() -> "PersonalityConfig | None":
    global _personality_cache, _personality_cache_time
    now = time.monotonic()
    if _personality_cache is not None and (now - _personality_cache_time) < _PERSONALITY_CACHE_TTL:
        return _personality_cache
    try:
        from serenity.config.loader import load_config
        _personality_cache = load_config().agents.defaults.personality
        _personality_cache_time = now
        return _personality_cache
    except Exception:
        return None


class ConversationDynamics:
    """Per-session state tracker. Created once, updated each turn."""

    def __init__(self, traits: list[str], session_key: str = "") -> None:
        self._session_key         = session_key
        self.emotion              = EmotionState(traits)
        self.turn                 = 0
        self.topic_turns          = 0
        self._last_topic          = ""
        self._last_msg_time       = time.time()
        self._last_response_chars = 0
        self.closure              = False
        self.truncated            = False
        self.greeting_energy: str | None = None
        self._speech_detector     = SpeechPatternDetector()
        self._emoji_detector      = EmojiStyleDetector()
        self.detected_modifier: str = "neutral"

        # Restore saved emotion state from disk so feelings carry across restarts.
        # If no saved state exists this is a no-op and she starts from trait defaults.
        if session_key:
            self._restore_from_disk()

    def _restore_from_disk(self) -> None:
        """Load last-saved emotion state from disk.

        Applies time-based decay for however long she was offline so the state
        feels natural rather than frozen — e.g. if energy was low before shutdown
        and she's been offline 20 minutes, it'll have drifted back a step toward
        her trait baseline by the time she wakes up.
        """
        store = _load_emotion_store()
        saved = store.get(self._session_key)
        if not saved:
            return
        self.emotion.apply_saved(saved)
        self.turn = int(saved.get("turn", 0))
        # Apply offline decay — time she was asleep counts as idle time
        saved_at = saved.get("saved_at", 0.0)
        if saved_at:
            offline_s = time.time() - float(saved_at)
            if offline_s > 0:
                self.emotion.decay(offline_s)

    def _snapshot(self) -> dict:
        """Build the dict that gets written to disk."""
        snap = self.emotion.to_dict()
        snap["turn"] = self.turn
        snap["saved_at"] = time.time()
        return snap

    def update_and_format(
        self,
        message: str,
        last_response_chars: int = 0,
    ) -> str:
        """Update state from this turn's signals and return the context block."""
        now = time.time()
        idle_s = now - self._last_msg_time
        self._last_msg_time = now
        self._last_response_chars = last_response_chars

        self.turn += 1
        self.closure            = _detect_closure(message)
        self.truncated          = _detect_truncation(message)
        self.greeting_energy    = _detect_greeting_energy(message)
        self.detected_modifier  = self._speech_detector.update(message)
        self._emoji_detector.update(message)

        # Topic tracking
        topic = _topic_hash(message)
        topic_changed = bool(topic) and topic != self._last_topic
        if topic_changed:
            self.topic_turns = 1
            self._last_topic = topic
        else:
            self.topic_turns += 1

        # Update emotion state
        self.emotion.update(
            last_response_chars=last_response_chars,
            time_since_last_s=idle_s,
            topic_turns=self.topic_turns,
            topic_changed=topic_changed,
        )

        # Build context block
        lines = [
            f"turn: {self.turn} | "
            f"topic_turns: {self.topic_turns} | "
            f"closure: {'yes' if self.closure else 'no'} | "
            f"truncated: {'yes' if self.truncated else 'no'}"
        ]

        if self.truncated:
            lines.append(
                "truncated: yes → Message appears cut off mid-sentence. "
                "Ask the user to finish their thought. Do not guess or complete it for them."
            )

        if self.greeting_energy:
            _ge = self.greeting_energy
            _own = self.emotion.energy
            # Mirror logic: user energy sets the ceiling, own energy sets the floor
            if _ge == "high" and _own == "high":
                lines.append(
                    "greeting: user high-energy → Match it fully. "
                    "Stretch the letters, be enthusiastic. e.g. 'Heyyyyyy!', 'YOOO'."
                )
            elif _ge == "high" and _own == "medium":
                lines.append(
                    "greeting: user high-energy, you medium → Meet them partway. "
                    "e.g. 'heyyy', 'yoo', warm but not maxed out."
                )
            elif _ge == "high" and _own == "low":
                lines.append(
                    "greeting: user high-energy, you low → Keep it real. "
                    "Short, genuine. e.g. 'hey', 'hey you'. Don't fake enthusiasm."
                )
            elif _ge == "medium" and _own != "low":
                lines.append(
                    "greeting: user medium-energy → Match casually. e.g. 'heyyy', 'yo'."
                )
            elif _ge == "low" or _own == "low":
                lines.append(
                    "greeting: low-energy → Short and genuine. e.g. 'hey', 'hi'."
                )

        if self.closure:
            lines.append(
                "closure: yes → Conversation winding down. "
                "Match energy. No new topics. No follow-up questions."
            )

        emoji_directive = self._emoji_detector.directive()
        if emoji_directive:
            lines.append(emoji_directive)

        directives = self.emotion.directives()
        lines.extend(directives)

        # Persist emotion snapshot so the next boot inherits this state.
        if self._session_key:
            _save_session_emotion(self._session_key, self._snapshot())

        return "[Conversation State]\n" + "\n".join(lines) + "\n[/Conversation State]"
