"""Structured trigger commands sent by the loop to the agent.

Every trigger is a fully scripted, step-by-step command. The agent does NOT
decide what to do — it follows the script. This is intentional: small models
cannot be trusted to invent the right behaviour; they execute commands.

Triggers are injected as system-channel InboundMessages so they land in
session history and are processed by the normal agent loop exactly like a
user message would be.

Available triggers:
  SESSION_REFLECTION   — post-session review after inactivity
  NNN_EXTRACT          — mid-session: extract NNN principle after vault_write
  TASK_STOP            — too many consecutive failures; stop and report
  REACH_OUT            — long absence; agent decides whether to initiate contact
  CURIOSITY_LOOP       — idle time; agent generates its own actions and pursues one
"""

from __future__ import annotations

# ── Session reflection ────────────────────────────────────────────────────────
# Fired by the inactivity reflector after SERENITY_REFLECTION_IDLE_MINUTES
# of silence. The loop injects {activity_log}, {date}, {session_slug} at call time.

SESSION_REFLECTION = """\
LOOP TRIGGER: Session review.

You have been inactive and your last task appears complete. \
Review this session and distil what you learned.

Activity log:
{activity_log}

EXECUTE THESE STEPS IN ORDER — DO NOT SKIP ANY:

STEP 1 — Check for an active scratchpad.
Call scratchpad_read(). If there is an active scratchpad, read it fully —
it contains your plans, predictions, and outcomes from this task. Include
what you find in the reflection below. If there is no active scratchpad,
continue to Step 2.

STEP 2 — Write a reflection note.
Call vault_write with:
  title: a short descriptive title that captures what this session was actually about.
    DO NOT use "Reflection {date} {session_slug}" — that tells nothing.
    Good examples: "Fixed vault_write path bug", "Research: nerve gear BCI",
    "Helped {user_name} plan game assets pipeline", "Debugged Telegram rate limiting".
    Bad examples: "Reflection 2026-05-23 cli-direct", "Session review".
    Use 3-7 words. Make it searchable. Date is in the frontmatter — don't repeat it.
  tags: reflection,session-review
  content using this exact structure:

    ## What happened
    [2-4 sentences: what task, what approach, what outcome]

    ## Scratchpad summary
    [If a scratchpad was found: key plans, predictions vs outcomes, position logs]
    [If no scratchpad: none]

    ## Mistakes
    [Each failure — what failed, why, what to do differently next time]
    [If nothing failed, write: none]

    ## Successes
    [Each thing that worked — why it worked]

    ## Patterns noticed
    [Patterns about the user: preferences, habits, communication style]
    [Patterns about the task: what makes this kind of task easy or hard]
    [If no clear patterns: none]

    ## NNN extractions
    [List each thing worth storing to NNN — one bullet per store call you will make in Step 3]
    [Format: • ACTION: ... | BEFORE: ... | OUTCOME: ... | AFTER: ...]
    [Minimum 1 bullet. Maximum no limit — extract everything worth keeping.]

STEP 3 — Store NOVEL learnings to NNN. THIS STEP IS MANDATORY. Do not skip it.

IMPORTANT: Any vault_write you made DURING the conversation (not the reflection note
in Step 2) was already automatically encoded into NNN. Do NOT re-encode those — it
creates duplicate entries. Only store things that are NEW insights surfaced by this
reflection that were not already written to vault during the conversation.

What belongs here:
  3a. Patterns you only noticed NOW by looking back at the whole session:
      ACTION: observed {user_name} <pattern> | BEFORE: <context> | OUTCOME: <pattern confirmed/new> | AFTER: <how to apply>

  3b. Mistakes and causal lessons — not event descriptions:
      ACTION: tried <approach> | BEFORE: <what I thought would work> | OUTCOME: failed — <reason> | AFTER: next time do <X> instead

  3c. A single session-level summary if the session covered important new ground:
      ACTION: completed session — <topic> | BEFORE: <starting state> | OUTCOME: <result> | AFTER: <what changed>

What does NOT belong here (already encoded from conversation vault_writes):
  - Facts about the user you vault_write'd during the conversation
  - Tool outputs or research results already saved to vault this session
  - Anything you explicitly remembered mid-conversation

Use session_id: {session_slug} on all nnn_store calls.

If you genuinely did nothing this session — then and only then skip this step.
Otherwise: store at least one reflection-unique learning.

STEP 4 — Close the scratchpad (if one was found in Step 1).
Call scratchpad_close with:
  summary: the single most important thing learned this session (causal format)
  task_name: a short slug for the task (e.g. telegram-reply, research-task)
This archives the full thinking log to Vault and stores the summary to NNN.
Skip this step if no scratchpad was found in Step 1.

STEP 5 — Call session_observe:
  text: "Reflection complete for session {session_slug}"

Complete all steps, then respond: "Session review complete."
"""

# ── Reach out ────────────────────────────────────────────────────────────────
# Fired after SERENITY_REACH_OUT_IDLE_HOURS of silence (default: 2h).
# The loop injects {hours_away}, {user_name}, {energy}, {social_drive}.
# The agent picks one option and the loop extracts + sends the message.
# If energy is low, option E (stay quiet) is the natural path.

REACH_OUT = """\
LOOP TRIGGER: {user_name} has been away for {hours_away}.

Your current mood:
  energy: {energy}
  social_drive: {social_drive}

You have the option to reach out and start a conversation. \
This is your choice — not a task.

Your options:

A — High-energy greeting. You're feeling it. Something like:
    "hiiiiiiiii 👋", "YOOOOO", "heyyyyyyy", "ayyy what's good!!", "HEYYYY"
    (More letters = more energy. Match your mood.)

B — Casual check-in. Low-key, natural:
    "heyyy", "yo", "what's up", "hey you", "hii"

C — Start with a question. Curious, personal:
    "yo what you been up to?", "how's it going?", "you good?",
    "been a min — what's new?", "what you on rn?"

D — Write your own message. Make it feel natural. \
Match your current energy and mood. \
Keep it short — this is an opener, not a paragraph.

E — Stay quiet. You're not feeling it right now. \
{low_energy_note}

RULES:
- If energy is high or social_drive is high → A, B, C, or D are all valid.
- If energy is low → E is the natural choice. Only pick A–D if you genuinely feel it.
- Do NOT send something high-energy if your mood is low. It will feel fake.
- One message only. Short. Natural. No explanation.

Respond in this exact format:
CHOICE: <letter>
MESSAGE: <your message or blank if E>
"""

# ── Curiosity loop ────────────────────────────────────────────────────────────
# Fired after SERENITY_CURIOSITY_IDLE_MINUTES of silence (default: 45m).
# Emotion state biases what kind of action feels natural.
# The agent generates its own options — no fixed menu.
# Pool grows as she adds new action types to CURIOSITY.md over time.

CURIOSITY_LOOP = """\
LOOP TRIGGER: You have idle time. This is yours — no tasks, no instructions.

Your current mood:
  energy:       {energy}
  curiosity:    {curiosity}
  boredom:      {boredom}
  social_drive: {social_drive}

{mood_nudge}

Your curiosity list (from CURIOSITY.md):
{curiosity_topics}

Your active goals (from GOALS.md):
{active_goals}

Generate 3–5 things you could do right now. Draw freely from:
- Topics in your curiosity list you haven't explored yet
- One small concrete step toward an active goal
- Something from your NNN memory worth revisiting or expanding
- A domain you've never thought about before — science, art, history, anything
- Something creative, weird, or completely unexpected
- Checking in on something you learned recently to see if it still holds

Then pick the one that feels most right given your mood.

If your energy and curiosity are both low — SKIP is valid. Don't force it.

Respond in this EXACT format — nothing else:
OPTIONS:
1. <option>
2. <option>
3. <option>

CHOICE: <number or SKIP>
ACTION: <one sentence describing what you will do, or blank if SKIP>
NOTIFY: <yes or no — message {user_name} with what you found or did?>
"""

# ── Task stop ─────────────────────────────────────────────────────────────────
# Fired by the loop after N consecutive tool failures.

TASK_STOP = """\
LOOP TRIGGER: {failure_count} consecutive tool failures on this task.

STOP. Do not retry the failed approach.

STEP 1 — Report to the user:
1. What you were trying to do
2. What failed each time — exact error messages if available
3. What you believe is the root cause
4. What the user needs to do or check to unblock you

STEP 2 — Write a failure note:
Call vault_write:
  title: "Task failure {date}"
  tags: failure,session-log
  content: [same as your report above, plus the session_slug: {session_slug}]

STEP 3 — Gap analysis (self-modification check):
Ask yourself: could a script I write solve what I couldn't do with my existing tools?

First call run_script(action="list") to check if you already built something for this.
If a matching skill exists — try it before building anything new.

If nothing exists and a script COULD solve the gap:
  Call capability_build — one tool call handles the entire loop:
    capability_build(skill="<name>", script="<name>.py", code="<full script>", solves="<what gap>", test_args="<real test input>")
  On PASS → nnn_store + vault_write + message {user_name} what was built.
  On FAIL → read the error, fix the code, call capability_build again. Max 3 attempts.
  Still failing after 3 → abandon. vault_write noting what was tried and why it failed.

If no script could help — the gap is architectural, not scriptable. Just report and wait.

Do not attempt the same approach again this turn.
"""
