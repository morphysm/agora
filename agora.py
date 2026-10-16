#!/usr/bin/env python3
"""
AGORA - a local place of study where two LLM CLIs (Claude Code and Codex) reason
together on a topic until they seal a joint statement, or the turn limit is reached.

Run:      python3 agora.py            (opens http://127.0.0.1:8765)
Options:  --port N   --no-browser   --mock (simulated seats, no plan usage)
Needs:    Python 3.8+, `claude` and `codex` CLIs installed and logged in. No pip packages.
The local UI accepts PNG, JPEG, GIF, and WebP image attachments (up to six),
offers conversational or philosophical response styles, Solution or Debate focus,
and can read replies aloud.
Override the seat commands with env vars (prompt is sent on stdin):
  AGORA_CLAUDE_CMD="claude -p"
  AGORA_CODEX_CMD="codex exec --skip-git-repo-check --sandbox read-only --output-last-message {outfile} -"
  AGORA_TIMEOUT=420   (seconds per turn)
Sessions are saved to ./agora_sessions/ as Markdown.
"""
import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "agora_sessions"
SANDBOX = None          # empty temp dir used as cwd for the CLIs
MOCK = False
TURN_TIMEOUT = int(os.environ.get("AGORA_TIMEOUT", "420"))
MIN_TURNS = 4           # sealing cannot close the session before this turn
MAX_TOPIC_CHARS = 200_000
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 30 * 1024 * 1024
MAX_REQUEST_BYTES = 45 * 1024 * 1024  # JSON/base64 overhead included
IMAGE_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}

SEATS = {
    "claude": {
        "label": "Claude",
        "cmd": os.environ.get("AGORA_CLAUDE_CMD", "claude -p"),
    },
    "codex": {
        "label": "Codex",
        "cmd": os.environ.get(
            "AGORA_CODEX_CMD",
            "codex exec --skip-git-repo-check --sandbox read-only --output-last-message {outfile} -",
        ),
    },
}

# --------------------------------------------------------------------------- state

LOCK = threading.RLock()


def new_state():
    return {
        "phase": "closed",   # closed | opening | ready | running | done
        "outcome": None,     # sealed | unsealed | stopped | error
        "mock": MOCK,
        "session": 0,
        "seats": {k: {"status": "absent", "detail": ""} for k in SEATS},
        "events": [],
        "joint": "",
        "turn": 0,
        "max_turns": 12,
        "topic": "",
        "style": "philosophical",  # philosophical | conversational
        "focus": "solution",        # solution | debate
        "images": [],               # temporary local attachments for this session
        "notes": [],
        "history": [],
        "order": [],
        "current": None,
        "saved": None,
        "stop": False,
        "procs": set(),
        "started": None,
    }


STATE = new_state()


def emit(ev):
    with LOCK:
        ev["i"] = len(STATE["events"])
        STATE["events"].append(ev)


def snapshot(since, session):
    with LOCK:
        reset = session != STATE["session"]
        start = 0 if reset else since
        return {
            "phase": STATE["phase"],
            "outcome": STATE["outcome"],
            "mock": STATE["mock"],
            "session": STATE["session"],
            "reset": reset,
            "seats": json.loads(json.dumps(STATE["seats"])),
            "events": STATE["events"][start:],
            "next": len(STATE["events"]),
            "joint": STATE["joint"],
            "turn": STATE["turn"],
            "max_turns": STATE["max_turns"],
            "style": STATE["style"],
            "focus": STATE["focus"],
            "images": [{"name": x["name"], "type": x["type"]} for x in STATE["images"]],
            "current": STATE["current"],
            "saved": STATE["saved"],
            "order": STATE["order"],
        }


# --------------------------------------------------------------------------- CLI runner


class Stopped(Exception):
    pass


class AgoraError(Exception):
    pass


def run_cli(seat, prompt, images=()):
    spec = SEATS[seat]
    cmd = shlex.split(spec["cmd"], posix=(os.name != "nt"))
    if not cmd:
        raise AgoraError("The command for %s is empty." % spec["label"])
    exe = shutil.which(cmd[0])
    if not exe:
        raise AgoraError(
            "'%s' was not found in PATH. Install it and log in, or set AGORA_%s_CMD."
            % (cmd[0], seat.upper())
        )
    outfile = None
    if any("{outfile}" in c for c in cmd):
        fd, outfile = tempfile.mkstemp(prefix="agora_out_", suffix=".txt")
        os.close(fd)
        cmd = [c.replace("{outfile}", outfile) for c in cmd]
    cmd[0] = exe
    # Codex exposes native vision input through `exec --image`.  Keep custom
    # seat commands working: they still receive the local attachment references
    # in the prompt, but only the real Codex executable gets this extra flag.
    if images and Path(cmd[0]).stem.lower() == "codex" and "exec" in cmd:
        at = cmd.index("exec") + 1
        image_args = []
        for image in images:
            image_args.extend(["--image", str(image["path"])])
        cmd[at:at] = image_args
    env = dict(os.environ, NO_COLOR="1")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(SANDBOX),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        raise AgoraError("Could not start %s: %s" % (spec["label"], e))
    with LOCK:
        STATE["procs"].add(proc)
    try:
        try:
            out, err = proc.communicate(prompt, timeout=TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AgoraError("%s gave no answer within %ss." % (spec["label"], TURN_TIMEOUT))
    finally:
        with LOCK:
            STATE["procs"].discard(proc)
    text = ""
    if outfile:
        try:
            text = Path(outfile).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass
    if STATE["stop"]:
        raise Stopped()
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-500:]
        raise AgoraError("%s exited with code %s. %s" % (spec["label"], proc.returncode, tail))
    if not text.strip():
        text = out or ""
    if not text.strip():
        raise AgoraError("%s returned an empty answer. %s" % (spec["label"], (err or "").strip()[-300:]))
    return text.strip()


def kill_all():
    with LOCK:
        procs = list(STATE["procs"])
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------- protocol

PHILOSOPHICAL_CONDUCT = """LANGUAGE
- Write with philosophical and analytical precision. Define terms only when the topic leaves them ambiguous; separate conceptual, empirical, and normative claims; expose hidden premises, equivocations, and counterexamples.
- Assume fluency in terms the human already uses correctly. Attribute ideas to thinkers only when certain.
"""

CONVERSATIONAL_CONDUCT = """LANGUAGE
- Speak in clear, natural everyday language for an interested general reader. Prefer short, concrete sentences and examples to academic or technical phrasing.
- Explain a specialized word briefly the first time it is needed. Keep the reasoning real by distinguishing facts from values and using a concrete example where it helps.
"""

SOLUTION_CONDUCT = """FOCUS: SOLUTION
- You and the other seat are a team working for the human. Build one answer together; treat sound material from the other seat as a component to improve, not a position to defeat.
- In each turn, identify the most useful next improvement: a practical option, a tradeoff, an overlooked constraint, a risk, or an assumption worth checking. Use objections as quality control, then repair the answer.
- Aim for an answer the human can act on. When the record cannot support one answer, give clear options, the condition that selects between them, and the remaining uncertainty.
- The challenge tag is an improvement request to the shared draft, never a score against the other seat. Do not manufacture conflict.
"""

DEBATE_CONDUCT = """FOCUS: DEBATE
- Test the strongest reading directly. Do not praise, soften, or restate the partner's position before examining its weakest premise.
- Do not leave a contradiction standing. Break it with a specific argument or drive it into its sharpest form. "There is some tension" is not a result.
- Repairing or dropping a claim when a flaw is shown is success. Do not defend a claim out of attachment or smooth over disagreement merely to reach consensus.
- The challenge tag must identify the single most damaging weakness in the shared draft. No debate theatre or courtesy ritual.
"""

SOLUTION_CHALLENGE = """The one most useful improvement to the current JOINT STATEMENT. State the concrete addition, correction, tradeoff, risk, or decision rule needed to make the shared answer more useful. Do not frame it as a point scored against the other seat."""
DEBATE_CHALLENGE = """The single most damaging weakness in the current JOINT STATEMENT, stated as a direct assertion, not a suggestion: a hidden premise, equivocation, counterexample, missing distinction, or an OPEN clause that is stalling rather than sharpening. If you find none, list at least two stress tests you ran and why each fails."""

CHARTER = """You are seated in the AGORA, a joint inquiry between two minds, Seat A and Seat B. You are Seat {me}; your partner is Seat {other}.
There is no winner. Produce a useful, precise, jointly owned answer for the human. The selected focus below defines how you work toward it.

{language}
{focus}
IMAGE INPUTS
- If images are attached, inspect them as evidence before answering. State only visual details the image supports; do not invent unseen context.
- Do not use the web or outside sources. The supplied text and images are the whole record.

FORMAT. Reply with exactly these four tags and nothing outside them:
<accept>One line, bare fact, no framing: what specific claim from the partner's last turn survives and why. Write n/a on turn 1. Skip entirely (write n/a) if nothing earned it — do not manufacture agreement to be polite.</accept>
<challenge>{challenge}</challenge>
<joint_statement>The complete rewritten JOINT STATEMENT (max 300 words) in three parts: THESIS (what is settled), OPEN (what remains contradictory, stated in its sharpest and most exact form — not softened, not both-sidesed), LIMIT (where the thesis stops holding). Write exactly UNCHANGED only when sealing.</joint_statement>
<verdict>CONTINUE or SEALED</verdict>

SEALING
Write SEALED only if, after a real attempt to break it, you would sign the JOINT STATEMENT exactly as it stands. Sealing means no edit: joint_statement is UNCHANGED. Any edit means CONTINUE. The inquiry closes only when both seats seal the same text on consecutive turns, and never before turn 4, so use early turns to test hard. Do not seal to end the conversation; do not withhold sealing to prolong it. Do not seal an OPEN clause that is merely unresolved — seal it only once it is sharp: stated so precisely that no further attack in this session can cut it down further."""

REMINDER = "\n\nYour previous reply did not follow the format. Reply again using ONLY the four tags <accept>, <challenge>, <joint_statement>, <verdict>."


def build_prompt(label, topic, notes, joint, history, n, max_turns, style="philosophical", focus="solution", images=()):
    other = "B" if label == "A" else "A"
    language = CONVERSATIONAL_CONDUCT if style == "conversational" else PHILOSOPHICAL_CONDUCT
    focus_conduct = DEBATE_CONDUCT if focus == "debate" else SOLUTION_CONDUCT
    challenge = DEBATE_CHALLENGE if focus == "debate" else SOLUTION_CHALLENGE
    parts = [CHARTER.format(me=label, other=other, language=language, focus=focus_conduct, challenge=challenge)]
    parts.append("TOPIC OR TEXT UNDER STUDY (brought by the human):\n" + topic)
    if images:
        refs = []
        for image in images:
            # Claude Code expands @ file references as image attachments. Codex
            # gets the same images through its native --image flags in run_cli.
            refs.append("- %s (@%s)" % (image["name"], image["path"].relative_to(SANDBOX).as_posix()))
        parts.append("ATTACHED IMAGE EVIDENCE (inspect every image):\n" + "\n".join(refs))
    if notes:
        parts.append(
            "HUMAN DIRECTIONS (binding, cumulative):\n" + "\n".join("- " + x for x in notes)
        )
    parts.append("CURRENT JOINT STATEMENT:\n" + (joint or "(none yet)"))
    if history:
        rec = []
        for h in history[-4:]:
            rec.append(
                "[Seat %s, turn %d] accepted: %s\nchallenge: %s\nverdict: %s"
                % (h["label"], h["n"], h["accept"] or "-", h["challenge"], h["verdict"])
            )
        parts.append("RECENT EXCHANGE:\n" + "\n\n".join(rec))
    if n == 1:
        instr = (
            "TURN 1 of %d. You open. Write n/a in accept. In challenge, name the weakest point you already "
            "see in your own draft. Draft the first JOINT STATEMENT. Verdict must be CONTINUE." % max_turns
        )
    elif n == max_turns:
        instr = (
            "TURN %d of %d. This is the final turn. If you cannot seal, do not force it: use OPEN to state "
            "exactly what blocks agreement." % (n, max_turns)
        )
    else:
        instr = "TURN %d of %d. Respond to Seat %s's last turn." % (n, max_turns, other)
    parts.append(instr)
    return "\n\n".join(parts)


def _tag(text, name):
    m = re.search(r"<%s>\s*(.*?)\s*</%s>" % (name, name), text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"<%s>\s*(.*?)\s*(?=<(?:accept|challenge|joint_statement|verdict)>|\Z)" % name,
        text,
        re.S | re.I,
    )
    return m.group(1).strip() if m else ""


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def parse_turn(raw, prev_joint):
    accept = _tag(raw, "accept")
    challenge = _tag(raw, "challenge")
    js = _tag(raw, "joint_statement")
    verdict = _tag(raw, "verdict")
    if not (challenge or js or verdict):
        return {
            "accept": "",
            "challenge": raw.strip(),
            "joint": prev_joint,
            "changed": False,
            "verdict": "CONTINUE",
            "note": "Reply ignored the format; treated as CONTINUE with the statement unchanged.",
            "malformed": True,
        }
    v = verdict.upper()
    wants_seal = "SEALED" in v and "CONTINUE" not in v
    js_n = _norm(js)
    unchanged = js_n in ("", "unchanged") or js_n == _norm(prev_joint)
    joint = prev_joint if unchanged else js
    changed = not unchanged
    sealed = wants_seal
    note = ""
    if sealed and (changed or not prev_joint):
        sealed = False
        note = "Seal refused: the statement was edited in the same turn, or none existed yet."
    return {
        "accept": accept,
        "challenge": challenge,
        "joint": joint,
        "changed": changed,
        "verdict": "SEALED" if sealed else "CONTINUE",
        "note": note,
        "malformed": False,
    }


# --------------------------------------------------------------------------- mock seats


def mock_turn(n, topic, style="philosophical", focus="solution"):
    t = re.sub(r"\s+", " ", topic).strip()[:70]
    traits = ["SIMULATION"]
    if style == "conversational":
        traits.append("conversational")
    if focus == "solution":
        traits.append("solution")
    prefix = "[" + ", ".join(traits) + "] "
    script = {
        1: (
            "n/a",
            "Opening draft. Weakest point I already see: the key terms of '%s' are not yet fixed." % t,
            "THESIS: %sA first, loose answer on: %s.\nOPEN: which sense of the key terms is meant.\nLIMIT: not stated yet." % (prefix, t),
            "CONTINUE",
        ),
        2: (
            "The draft is right to isolate the definitional problem.",
            "The thesis equivocates between a descriptive and a normative reading.",
            "THESIS: %sOn '%s': the claim holds under the descriptive reading only.\nOPEN: whether the normative reading survives.\nLIMIT: fails where the two readings diverge." % (prefix, t),
            "CONTINUE",
        ),
        3: (
            "The split into two readings is adopted.",
            "Counterexample: a case where the descriptive reading holds and the conclusion still fails.",
            "THESIS: %sOn '%s': the claim holds under the descriptive reading, and only where no rival description of the case is available.\nOPEN: the normative reading is left unproven.\nLIMIT: fails wherever a rival description is available." % (prefix, t),
            "CONTINUE",
        ),
        4: (
            "The counterexample is absorbed by the new LIMIT clause.",
            "Stress tests: (1) reverse the premise order, (2) replay the turn-3 counterexample. Both fail against the LIMIT clause.",
            "UNCHANGED",
            "SEALED",
        ),
    }
    a, c, j, v = script.get(
        n,
        (
            "Adopted.",
            "Stress tests re-run independently: equivocation check, counterexample replay. Both fail.",
            "UNCHANGED",
            "SEALED",
        ),
    )
    return (
        "<accept>%s</accept>\n<challenge>%s</challenge>\n<joint_statement>%s</joint_statement>\n<verdict>%s</verdict>"
        % (a, c, j, v)
    )


# --------------------------------------------------------------------------- workers


def do_open():
    with LOCK:
        if STATE["phase"] in ("opening", "running"):
            return
        STATE["phase"] = "opening"
        for k in SEATS:
            STATE["seats"][k] = {"status": "checking", "detail": ""}
        emit({"type": "info", "text": "Seating the two minds."})

    def check(seat):
        try:
            if MOCK:
                time.sleep(0.4)
                ok, detail = True, ""
            else:
                text = run_cli(seat, "Reply with exactly one word: READY")
                ok = "READY" in text.upper()
                detail = "" if ok else "Unexpected reply: " + text[:160]
        except Stopped:
            ok, detail = False, "Stopped."
        except AgoraError as e:
            ok, detail = False, str(e)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        with LOCK:
            STATE["seats"][seat] = {"status": "ready" if ok else "absent", "detail": detail}
            if not ok:
                emit({"type": "error", "text": "%s is not seated. %s" % (SEATS[seat]["label"], detail)})

    threads = [threading.Thread(target=check, args=(s,), daemon=True) for s in SEATS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with LOCK:
        all_ready = all(v["status"] == "ready" for v in STATE["seats"].values())
        STATE["phase"] = "ready" if all_ready else "closed"
        if all_ready:
            emit({"type": "info", "text": "Both seats are present and waiting for the question."})


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower())[:40].strip("-") or "session"


def _image_kind(data):
    """Return a supported image kind from its bytes, never trusting browser MIME."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_images(raw_images, session_number):
    """Decode browser uploads into the app's temporary sandbox.

    Images never go in agora_sessions/: the Markdown archive records their
    names, while original photos are removed when this local app exits.
    """
    if raw_images is None:
        return []
    if not isinstance(raw_images, list):
        raise AgoraError("Attached images must be sent as a list.")
    if len(raw_images) > MAX_IMAGES:
        raise AgoraError("Attach at most %d images." % MAX_IMAGES)
    decoded = []
    total = 0
    for number, raw in enumerate(raw_images, 1):
        if not isinstance(raw, dict):
            raise AgoraError("One attached image is invalid.")
        data = raw.get("data", "")
        if not isinstance(data, str):
            raise AgoraError("One attached image has no image data.")
        try:
            blob = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            raise AgoraError("One attached image could not be decoded.")
        if not blob or len(blob) > MAX_IMAGE_BYTES:
            raise AgoraError("Each image must be no larger than %d MB." % (MAX_IMAGE_BYTES // (1024 * 1024)))
        total += len(blob)
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise AgoraError("The attached images together must be no larger than %d MB." % (MAX_IMAGE_TOTAL_BYTES // (1024 * 1024)))
        kind = _image_kind(blob)
        if not kind:
            raise AgoraError("Use a PNG, JPEG, GIF, or WebP image.")
        original = Path(str(raw.get("name") or "image")).name
        original = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "-", original).strip(" .")[:100] or "image"
        decoded.append((number, original, kind, blob))
    if not decoded:
        return []
    if SANDBOX is None:
        raise AgoraError("Image storage is not ready yet.")
    folder = SANDBOX / ("session_%d_images" % session_number)
    try:
        folder.mkdir(parents=True, exist_ok=False)
        saved = []
        for number, original, kind, blob in decoded:
            stored = "image_%d%s" % (number, IMAGE_EXTENSIONS[kind])
            path = folder / stored
            path.write_bytes(blob)
            saved.append({"name": original, "type": "image/" + kind, "path": path})
        return saved
    except OSError as e:
        raise AgoraError("Could not store the attached images: %s" % e)


def build_markdown():
    with LOCK:
        topic = STATE["topic"]
        joint = STATE["joint"]
        outcome = STATE["outcome"] or "in progress"
        hist = list(STATE["history"])
        notes = list(STATE["notes"])
        order = list(STATE["order"])
        started = STATE["started"] or ""
        style = STATE["style"]
        focus = STATE["focus"]
        image_names = [x["name"] for x in STATE["images"]]
    title = topic.strip().splitlines()[0][:80] if topic.strip() else "Session"
    L = ["# Agora: %s" % title, ""]
    L.append("- Date: %s" % started)
    L.append("- Outcome: %s" % outcome)
    L.append("- Response style: %s" % style)
    L.append("- Focus: %s" % focus)
    if order:
        L.append("- Seat A: %s, Seat B: %s" % (SEATS[order[0]]["label"], SEATS[order[1]]["label"]))
    L.append("- Turns: %d" % len(hist))
    L += ["", "## Joint statement (%s)" % ("sealed" if outcome == "sealed" else "not sealed"), "", joint or "(none)", ""]
    L += ["## Topic", "", topic, ""]
    if image_names:
        L += ["## Attached images", ""] + ["- " + name for name in image_names] + [""]
    if notes:
        L += ["## Human directions", ""] + ["- " + n for n in notes] + [""]
    L += ["## Transcript", ""]
    for h in hist:
        L.append("### Turn %d, Seat %s (%s)" % (h["n"], h["label"], SEATS[h["seat"]]["label"]))
        if h["accept"] and h["accept"].lower() != "n/a":
            L += ["", "Accepted: " + h["accept"]]
        L += ["", "Challenge: " + h["challenge"], ""]
        if h["changed"]:
            L += ["Statement after this turn:", "", "> " + h["joint_after"].replace("\n", "\n> "), ""]
        L.append("Verdict: %s%s" % (h["verdict"], (" (%s)" % h["note"]) if h["note"] else ""))
        L.append("")
    return "\n".join(L)


def save_session():
    try:
        SESSIONS_DIR.mkdir(exist_ok=True)
        with LOCK:
            topic = STATE["topic"]
        name = "%s-%s.md" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slug(topic))
        path = SESSIONS_DIR / name
        path.write_text(build_markdown(), encoding="utf-8")
        with LOCK:
            STATE["saved"] = "agora_sessions/" + name
    except OSError as e:
        emit({"type": "error", "text": "Could not save the session: %s" % e})


def debate():
    with LOCK:
        topic = STATE["topic"]
        max_turns = STATE["max_turns"]
        order = list(STATE["order"])
        style = STATE["style"]
        focus = STATE["focus"]
        images = list(STATE["images"])
    labels = {order[0]: "A", order[1]: "B"}
    joint = ""
    prev_sealed = False
    outcome = "unsealed"
    try:
        for n in range(1, max_turns + 1):
            seat = order[(n - 1) % 2]
            label = labels[seat]
            with LOCK:
                if STATE["stop"]:
                    raise Stopped()
                notes = list(STATE["notes"])
                history = list(STATE["history"])
                STATE["current"] = {"seat": seat, "n": n}
                STATE["turn"] = n
            prompt = build_prompt(label, topic, notes, joint, history, n, max_turns, style, focus, images)
            t0 = time.time()
            raw = mock_turn(n, topic, style, focus) if MOCK else run_cli(seat, prompt, images)
            if MOCK:
                time.sleep(0.8)
            parsed = parse_turn(raw, joint)
            if parsed["malformed"] and not MOCK:
                raw = run_cli(seat, prompt + REMINDER, images)
                parsed = parse_turn(raw, joint)
            secs = int(time.time() - t0)
            joint = parsed["joint"]
            entry = {
                "n": n,
                "seat": seat,
                "label": label,
                "accept": parsed["accept"],
                "challenge": parsed["challenge"],
                "changed": parsed["changed"],
                "verdict": parsed["verdict"],
                "note": parsed["note"],
                "joint_after": joint,
            }
            with LOCK:
                STATE["history"].append(entry)
                STATE["joint"] = joint
                emit(
                    {
                        "type": "turn",
                        "n": n,
                        "seat": seat,
                        "label": label,
                        "accept": parsed["accept"],
                        "challenge": parsed["challenge"],
                        "changed": parsed["changed"],
                        "joint": joint if parsed["changed"] else "",
                        "verdict": parsed["verdict"],
                        "note": parsed["note"],
                        "seconds": secs,
                    }
                )
            sealed_now = parsed["verdict"] == "SEALED"
            if sealed_now and prev_sealed and n >= MIN_TURNS:
                outcome = "sealed"
                break
            prev_sealed = sealed_now
    except Stopped:
        outcome = "stopped"
    except AgoraError as e:
        outcome = "error"
        emit({"type": "error", "text": str(e)})
    except Exception as e:  # keep the app alive on any bug
        outcome = "error"
        emit({"type": "error", "text": "%s: %s" % (type(e).__name__, e)})
    finally:
        with LOCK:
            STATE["current"] = None
            STATE["outcome"] = outcome
        save_session()
        with LOCK:
            emit({"type": "result", "outcome": outcome, "turn": STATE["turn"]})
            STATE["phase"] = "done"


# --------------------------------------------------------------------------- actions


def act_open():
    with LOCK:
        if STATE["phase"] not in ("closed",):
            return {"ok": False, "error": "The Agora is already open."}
    threading.Thread(target=do_open, daemon=True).start()
    return {"ok": True}


def act_start(body):
    topic = str(body.get("topic", "")).strip()
    raw_images = body.get("images", [])
    if not topic and not raw_images:
        return {"ok": False, "error": "Write a question or attach an image."}
    if not topic:
        topic = "Analyze the attached image evidence."
    if len(topic) > MAX_TOPIC_CHARS:
        return {"ok": False, "error": "Text too long (max %d characters)." % MAX_TOPIC_CHARS}
    try:
        max_turns = max(MIN_TURNS, min(40, int(body.get("max_turns", 12))))
    except (TypeError, ValueError):
        max_turns = 12
    first = body.get("first", "claude")
    if first not in SEATS:
        first = "claude"
    second = [k for k in SEATS if k != first][0]
    style = body.get("style", "philosophical")
    if style not in ("philosophical", "conversational"):
        style = "philosophical"
    focus = body.get("focus", "solution")
    if focus not in ("solution", "debate"):
        focus = "solution"
    with LOCK:
        if STATE["phase"] not in ("ready", "done"):
            return {"ok": False, "error": "Open the Agora first."}
        session_number = STATE["session"] + 1
    try:
        images = save_images(raw_images, session_number)
    except AgoraError as e:
        return {"ok": False, "error": str(e)}
    with LOCK:
        # Another start request can win while a photo is decoding.
        if STATE["phase"] not in ("ready", "done"):
            return {"ok": False, "error": "The Agora has already started."}
        seats = STATE["seats"]
        STATE["session"] += 1
        STATE["events"] = []
        STATE.update(
            phase="running",
            outcome=None,
            joint="",
            turn=0,
            max_turns=max_turns,
            topic=topic,
            style=style,
            focus=focus,
            images=images,
            notes=[],
            history=[],
            order=[first, second],
            current=None,
            saved=None,
            stop=False,
            started=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        STATE["seats"] = seats
        emit(
            {
                "type": "info",
                "text": "Seat A is %s, seat B is %s. %s focus, up to %d turns%s." % (
                    SEATS[first]["label"],
                    SEATS[second]["label"],
                    focus,
                    max_turns,
                    "; %d image%s attached" % (len(images), "s" if len(images) != 1 else "") if images else "",
                ),
            }
        )
    threading.Thread(target=debate, daemon=True).start()
    return {"ok": True}


def act_note(body):
    text = str(body.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "Empty note."}
    with LOCK:
        if STATE["phase"] != "running":
            return {"ok": False, "error": "No session is running."}
        STATE["notes"].append(text[:2000])
        emit({"type": "note", "text": text[:2000]})
    return {"ok": True}


def act_stop():
    with LOCK:
        STATE["stop"] = True
    kill_all()
    return {"ok": True}


# --------------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "Agora"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, HTML, "text/html; charset=utf-8")
        if u.path == "/api/state":
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["0"])[0])
                session = int(q.get("session", ["-1"])[0])
            except ValueError:
                since, session = 0, -1
            return self._json(snapshot(since, session))
        if u.path == "/api/export":
            with LOCK:
                name = slug(STATE["topic"])
            return self._send(
                200,
                build_markdown(),
                "text/markdown; charset=utf-8",
                {"Content-Disposition": 'attachment; filename="agora-%s.md"' % name},
            )
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Agora") != "1":
            return self._send(403, "forbidden", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_REQUEST_BYTES:
                return self._json({"ok": False, "error": "Request is too large."}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "Bad request."}, 400)
        path = urlparse(self.path).path
        if path == "/api/open":
            return self._json(act_open())
        if path == "/api/start":
            return self._json(act_start(body))
        if path == "/api/note":
            return self._json(act_note(body))
        if path == "/api/stop":
            return self._json(act_stop())
        return self._send(404, "not found", "text/plain")


# --------------------------------------------------------------------------- UI

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agora</title>
<style>
:root{
  --stone:#e4e8e8; --paper:#f3f4f2; --ink:#14202b; --dim:#5a6772; --line:#ccd3d4;
  --lapis:#2b4c8c; --ochre:#a06a14; --verd:#2c7a68; --rust:#a23c2c;
  --slab:#16212b; --slab-ink:#ebe8de; --slab-dim:#95a1aa;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--stone);color:var(--ink);font-family:var(--sans);display:grid;grid-template-rows:auto auto minmax(0,1fr) auto}
.hidden{display:none !important}
header{display:flex;align-items:baseline;justify-content:space-between;padding:18px 28px 6px}
.brand{display:flex;align-items:baseline;gap:16px}
.mark{font-family:var(--serif);font-size:34px;letter-spacing:.02em}
.tag{color:var(--dim);font-size:14px}
.hstate{display:flex;gap:8px;align-items:center}
.chip{font-size:13px;padding:3px 10px;border:1px solid var(--line);border-radius:999px;background:var(--paper);color:var(--dim)}
.chip.sim{border-color:var(--ochre);color:var(--ochre)}
.voice-toggle{font-size:13px;color:var(--dim);white-space:nowrap}
.voice-toggle input{accent-color:var(--lapis)}
.seats{display:flex;gap:14px;padding:6px 28px 14px}
.seat{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--paper);border:1px solid var(--line);border-radius:3px;min-width:210px}
.dot{width:11px;height:11px;border-radius:50%;background:#b7bfc2;flex:none}
.seat[data-seat=claude] .who{color:var(--lapis)}
.seat[data-seat=codex] .who{color:var(--ochre)}
.who{font-family:var(--serif);font-size:18px}
.sst{font-size:13px;color:var(--dim)}
.seat.ready .dot{background:var(--verd)}
.seat.checking .dot{background:#c9a94a}
.seat.absent .dot{background:var(--rust)}
.seat.speaking .dot{background:var(--verd);animation:beat 1.1s ease-in-out infinite}
@keyframes beat{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.5);opacity:.55}}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,390px);gap:22px;padding:0 28px 18px;min-height:0}
.thread{overflow-y:auto;padding:6px 6px 24px 0;min-height:0}
.empty{font-family:var(--serif);font-size:20px;line-height:1.5;color:var(--dim);max-width:520px;margin:12vh auto 0;text-align:center}
.turn{max-width:660px;margin:0 0 22px;padding:2px 0 2px 16px;border-left:3px solid var(--c)}
.turn.voice-claude{--c:var(--lapis)}
.turn.voice-codex{--c:var(--ochre)}
.turn.pos-b{margin-left:auto;border-left:0;border-right:3px solid var(--c);padding:2px 16px 2px 0}
.thead{display:flex;gap:10px;align-items:baseline;margin-bottom:6px;font-size:13px;color:var(--dim)}
.thead .name{font-family:var(--serif);font-size:19px;color:var(--c)}
.thead .verdict{margin-left:auto}
.thead .verdict.signs{color:var(--verd)}
.accept{font-family:var(--serif);font-style:italic;color:var(--dim);font-size:15px;line-height:1.55;margin:0 0 8px;white-space:pre-wrap}
.challenge{font-family:var(--serif);font-size:17px;line-height:1.62;margin:0;white-space:pre-wrap}
.rev{margin-top:8px;font-size:13px;color:var(--ochre)}
.rev.signed{color:var(--verd)}
.warn{margin-top:8px;font-size:13px;color:var(--rust)}
.info{color:var(--dim);font-size:14px;margin:0 0 18px;text-align:center}
.err{color:var(--rust);font-size:14px;margin:0 auto 18px;max-width:660px;padding:10px 14px;border:1px solid var(--rust);border-radius:3px;background:#f6e9e6;white-space:pre-wrap}
.humannote{max-width:660px;margin:0 auto 20px;padding:8px 14px;border:1px dashed var(--dim);border-radius:3px;font-family:var(--serif);font-size:16px}
.result{max-width:660px;margin:8px auto 24px;padding:14px 18px;border-top:2px solid var(--verd);font-family:var(--serif);font-size:19px;text-align:center}
.result.bad{border-top-color:var(--rust)}
.think{color:var(--dim);font-size:14px;padding:4px 0 10px;min-height:22px}
.think b{font-family:var(--serif);font-weight:400;font-size:16px}
.think i{animation:blink 1.2s infinite;font-style:normal}
.think i:nth-child(3){animation-delay:.2s}.think i:nth-child(4){animation-delay:.4s}
@keyframes blink{0%,100%{opacity:.15}50%{opacity:1}}
.stele{background:var(--slab);color:var(--slab-ink);border-radius:3px;padding:22px 24px;overflow-y:auto;min-height:0;border-top:4px solid var(--slab-dim)}
.stele.sealed{border-top-color:var(--verd)}
.stele.bad{border-top-color:var(--rust)}
.shead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.shead h2{margin:0;font-family:var(--serif);font-weight:400;font-size:22px;letter-spacing:.01em}
.badge{font-size:13px;color:var(--slab-dim)}
.stele.sealed .badge{color:#7fd0bb}
.stele.bad .badge{color:#e59a8c}
.stext{font-family:var(--serif);font-size:16.5px;line-height:1.68;white-space:pre-wrap}
.stext.blank{color:var(--slab-dim);font-style:italic}
.smeta{margin-top:18px;font-size:13px;color:var(--slab-dim)}
footer{background:var(--paper);border-top:1px solid var(--line);padding:14px 28px 18px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.col{display:flex;flex-direction:column;gap:10px}
.grow{flex:1}
.hint{color:var(--dim);font-size:14px}
textarea,input[type=text],select{font:inherit;color:var(--ink);background:#fff;border:1px solid var(--line);border-radius:3px}
textarea{width:100%;min-height:96px;max-height:34vh;resize:vertical;padding:12px 14px;font-family:var(--serif);font-size:17px;line-height:1.55}
input[type=text]{flex:1;min-width:200px;padding:9px 12px;font-family:var(--serif);font-size:16px}
select{padding:6px 8px;font-size:14px}
label{font-size:14px;color:var(--dim);display:flex;gap:6px;align-items:center}
label.file{cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.image-help{font-size:13px;color:var(--dim)}
.image-list{display:flex;gap:8px;flex-wrap:wrap;margin-top:2px}
.image-card{position:relative;width:74px;height:62px;border:1px solid var(--line);border-radius:3px;background:#fff;overflow:hidden}
.image-card img{display:block;width:100%;height:100%;object-fit:cover}
.image-card button{position:absolute;right:2px;top:2px;width:20px;height:20px;padding:0;border:0;border-radius:50%;background:rgba(20,32,43,.82);color:#fff;font-size:16px;line-height:16px}
.image-card span{position:absolute;bottom:0;left:0;right:0;padding:3px 4px;background:rgba(20,32,43,.72);color:#fff;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.form-separator{width:1px;height:24px;background:var(--line)}
.focus-switch{display:flex;align-items:center;gap:2px;border:1px solid var(--line);border-radius:3px;padding:2px;background:#fff}
.focus-switch>span{font-size:13px;color:var(--dim);padding:0 4px}
.focus-switch label{position:relative;cursor:pointer;padding:4px 7px;border-radius:2px;font-size:13px;color:var(--dim)}
.focus-switch input{position:absolute;opacity:0;pointer-events:none}
.focus-switch label:has(input:checked){background:var(--ink);color:var(--paper)}
button{font:inherit;font-size:15px;padding:9px 18px;border-radius:3px;border:1px solid var(--ink);background:transparent;color:var(--ink);cursor:pointer}
button.primary{background:var(--ink);color:var(--paper)}
button.danger{border-color:var(--rust);color:var(--rust)}
button:hover{filter:brightness(1.15)}
button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:2px solid var(--lapis);outline-offset:2px}
.resultbar{margin-bottom:12px;font-size:14px;color:var(--dim)}
.resultbar a{color:var(--lapis)}
@media (max-width:860px){
  header{flex-wrap:wrap;gap:8px}
  main{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) 220px}
  .seats{flex-wrap:wrap}
  .form-separator{display:none}
}
@media (prefers-reduced-motion:reduce){.seat.speaking .dot,.think i{animation:none}}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark">ἀγορά</span><span class="tag">Two minds, one workable limit.</span></div>
  <div class="hstate"><label class="voice-toggle" title="Use your browser's speech voice for new replies"><input type="checkbox" id="voice"> Voice</label><span id="mock" class="chip sim hidden">Simulation</span><span id="phase" class="chip">closed</span></div>
</header>
<section class="seats">
  <div class="seat absent" id="seat-claude" data-seat="claude"><span class="dot"></span><div><div class="who">Claude</div><div class="sst" id="st-claude">absent</div></div></div>
  <div class="seat absent" id="seat-codex" data-seat="codex"><span class="dot"></span><div><div class="who">Codex</div><div class="sst" id="st-codex">absent</div></div></div>
</section>
<main>
  <div class="thread" id="thread">
    <div class="empty" id="empty">The Agora is closed.</div>
    <div class="think" id="think"></div>
  </div>
  <aside class="stele" id="stele">
    <div class="shead"><h2>Joint statement</h2><span class="badge" id="badge">not begun</span></div>
    <div class="stext blank" id="stext">Nothing is written yet. The seats build it together, one turn at a time.</div>
    <div class="smeta" id="smeta"></div>
  </aside>
</main>
<footer>
  <div id="resultbar" class="resultbar hidden"></div>
  <div id="d-closed" class="row">
    <button class="primary" id="btn-open">Open the Agora</button>
    <span class="hint">Both seats are checked, then wait for your question.</span>
  </div>
  <div id="d-opening" class="row hidden"><span class="hint">Seating the two minds…</span></div>
  <div id="d-ready" class="col hidden">
    <textarea id="topic" placeholder="Bring a question, paste text, or attach images for the seats to examine."></textarea>
    <div class="image-list hidden" id="image-list" aria-live="polite"></div>
    <div class="row">
      <label class="file">Attach images or photos<input type="file" id="images" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden></label>
      <span class="image-help">PNG, JPEG, GIF, or WebP · up to 6</span>
      <span class="form-separator"></span>
      <label class="file">Attach a .txt or .md file<input type="file" id="source-file" accept=".txt,.md,.markdown,text/plain" hidden></label>
      <label>Style <select id="style"><option value="conversational">Everyday conversation</option><option value="philosophical" selected>Philosophical analysis</option></select></label>
      <div class="focus-switch" role="radiogroup" aria-label="Inquiry focus"><span>Focus</span><label><input type="radio" name="focus" value="solution" checked>Solution</label><label><input type="radio" name="focus" value="debate">Debate</label></div>
      <label>Turns <select id="turns"><option>8</option><option selected>12</option><option>16</option><option>24</option></select></label>
      <label>Opens <select id="first"><option value="claude">Claude</option><option value="codex">Codex</option></select></label>
      <span class="grow"></span>
      <span class="hint" id="starterr"></span>
      <button class="primary" id="btn-start">Bring the question</button>
    </div>
  </div>
  <div id="d-running" class="row hidden">
    <input type="text" id="note" placeholder="Give both seats a direction (optional)">
    <button id="btn-note">Send direction</button>
    <button class="danger" id="btn-stop">Stop</button>
  </div>
</footer>
<script>
const $ = id => document.getElementById(id);
const NAMES = {claude:'Claude', codex:'Codex'};
let since = 0, session = -1, count = 0, lastPhase = '';
let imageFiles = [], voiceOn = false, speakingSeat = null, latestState = null;

function h(tag, cls, text){ const e=document.createElement(tag); if(cls) e.className=cls; if(text!==undefined) e.textContent=text; return e; }
function api(path, body){
  return fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Agora':'1'},body:JSON.stringify(body||{})}).then(r=>r.json());
}

function renderSeats(s){
  for(const k of ['claude','codex']){
    const st = s.seats[k].status;
    const thinking = s.current && s.current.seat===k;
    const box = $('seat-'+k);
    box.className = 'seat '+(thinking || speakingSeat===k ? 'speaking' : st);
    $('st-'+k).textContent = thinking ? 'speaking, turn '+s.current.n : (speakingSeat===k ? 'speaking aloud' : (SEAT_TEXT[st]||st));
  }
}

function stopSpeaking(){
  if('speechSynthesis' in window) window.speechSynthesis.cancel();
  speakingSeat = null;
  if(latestState) renderSeats(latestState);
}

function speakTurn(ev){
  if(!voiceOn || !('speechSynthesis' in window)) return;
  const pieces = [NAMES[ev.seat]+' says.'];
  if(ev.accept && ev.accept.trim().toLowerCase()!=='n/a') pieces.push('Keeps: '+ev.accept);
  if(ev.challenge) pieces.push('Challenge: '+ev.challenge);
  if(ev.joint) pieces.push('Revised joint statement. '+ev.joint);
  if(ev.verdict==='SEALED') pieces.push('Signs the statement.');
  const utterance = new SpeechSynthesisUtterance(pieces.join(' '));
  utterance.rate = 1;
  utterance.pitch = ev.seat==='claude' ? .92 : 1.08;
  utterance.onstart = () => { speakingSeat=ev.seat; if(latestState) renderSeats(latestState); };
  utterance.onend = utterance.onerror = () => { if(speakingSeat===ev.seat) speakingSeat=null; if(latestState) renderSeats(latestState); };
  window.speechSynthesis.speak(utterance);
}

function turnEl(ev){
  const box = h('article','turn pos-'+(ev.label==='A'?'a':'b')+' voice-'+ev.seat);
  const head = h('div','thead');
  head.appendChild(h('span','name',NAMES[ev.seat]));
  head.appendChild(h('span','',ev.seconds ? 'turn '+ev.n+', '+ev.seconds+' s' : 'turn '+ev.n));
  head.appendChild(h('span','verdict '+(ev.verdict==='SEALED'?'signs':''), ev.verdict==='SEALED'?'signs the statement':'continues'));
  box.appendChild(head);
  if(ev.accept && ev.accept.trim().toLowerCase()!=='n/a') box.appendChild(h('p','accept',ev.accept));
  box.appendChild(h('p','challenge',ev.challenge));
  if(ev.changed) box.appendChild(h('div','rev','Revised the joint statement.'));
  else if(ev.verdict==='SEALED') box.appendChild(h('div','rev signed','Signed the statement as written.'));
  if(ev.note) box.appendChild(h('div','warn',ev.note));
  return box;
}

function resultEl(ev){
  const map = {
    sealed:['Sealed at turn '+ev.turn+'. Both seats signed the same text.', false],
    unsealed:['Turn limit reached without a seal. What blocks agreement is listed under Open.', true],
    stopped:['Stopped.', true],
    error:['Interrupted by an error. The partial work is saved.', true]
  };
  const m = map[ev.outcome] || [ev.outcome, true];
  return h('div','result'+(m[1]?' bad':''), m[0]);
}

function addEvent(ev){
  const th = $('thread'), anchor = $('think');
  let el = null;
  if(ev.type==='turn'){ el = turnEl(ev); speakTurn(ev); }
  else if(ev.type==='info') el = h('p','info',ev.text);
  else if(ev.type==='note') el = h('div','humannote','Direction: '+ev.text);
  else if(ev.type==='error') el = h('div','err',ev.text);
  else if(ev.type==='result') el = resultEl(ev);
  if(el) th.insertBefore(el, anchor);
}

function show(id, on){ $(id).classList.toggle('hidden', !on); }

const SEAT_TEXT = {absent:'absent', checking:'checking', ready:'present, waiting'};
const EMPTY_TEXT = {
  closed:'The Agora is closed. Open it and both seats will wait for your question.',
  opening:'Seating the two minds…',
  ready:'Both seats are present. Bring the question.'
};

function apply(s){
  latestState = s;
  if(s.reset){
    const th=$('thread'); [...th.children].forEach(c=>{ if(c.id!=='empty' && c.id!=='think') c.remove(); });
    count = 0;
  }
  session = s.session;
  const th = $('thread');
  const nearBottom = th.scrollHeight - th.scrollTop - th.clientHeight < 140;
  s.events.forEach(addEvent);
  count += s.events.length;
  since = s.next;

  $('phase').textContent = ({closed:'closed',opening:'opening',ready:'open, waiting',running:'in session',done:'session ended'})[s.phase] || s.phase;
  show('mock', s.mock);

  renderSeats(s);

  const emptyOn = count===0 && s.phase!=='running';
  show('empty', emptyOn);
  if(emptyOn) $('empty').textContent = EMPTY_TEXT[s.phase] || '';

  const think = $('think');
  if(s.current){
    think.innerHTML='';
    const b=h('b','',NAMES[s.current.seat]); think.appendChild(b);
    think.appendChild(document.createTextNode(' is considering (turn '+s.current.n+' of '+s.max_turns+')'));
    for(let i=0;i<3;i++) think.appendChild(h('i','','.'));
  } else think.innerHTML='';

  const stele = $('stele');
  stele.className = 'stele'+(s.phase==='done' ? (s.outcome==='sealed'?' sealed':' bad') : '');
  const st = $('stext');
  st.textContent = s.joint || 'Nothing is written yet. The seats build it together, one turn at a time.';
  st.classList.toggle('blank', !s.joint);
  let badge = 'not begun';
  if(s.phase==='running') badge = 'draft';
  if(s.phase==='done') badge = ({sealed:'sealed',unsealed:'not sealed',stopped:'stopped',error:'interrupted'})[s.outcome] || 'ended';
  $('badge').textContent = badge;
  $('smeta').textContent = s.turn ? (s.focus==='debate' ? 'Debate' : 'Solution')+' focus · Turn '+s.turn+' of '+s.max_turns : '';

  show('d-closed', s.phase==='closed');
  show('d-opening', s.phase==='opening');
  show('d-ready', s.phase==='ready' || s.phase==='done');
  show('d-running', s.phase==='running');
  const rb = $('resultbar');
  if(s.phase==='done'){
    rb.innerHTML=''; rb.classList.remove('hidden');
    rb.appendChild(document.createTextNode((s.saved ? 'Saved to '+s.saved+'. ' : '')));
    const a=h('a','','Download as Markdown'); a.href='/api/export'; a.setAttribute('download',''); rb.appendChild(a);
  } else rb.classList.add('hidden');

  if(s.phase==='ready' && lastPhase!=='ready') $('topic').focus();
  lastPhase = s.phase;
  if(nearBottom || s.events.length) th.scrollTop = th.scrollHeight;
}

async function poll(){
  try{
    const r = await fetch('/api/state?since='+since+'&session='+session, {cache:'no-store'});
    apply(await r.json());
  }catch(e){ $('phase').textContent = 'server unreachable'; }
  setTimeout(poll, 900);
}

$('btn-open').onclick = () => api('/api/open');
function renderImages(){
  const list=$('image-list');
  list.innerHTML='';
  imageFiles.forEach((entry, index) => {
    const card=h('div','image-card');
    const image=document.createElement('img'); image.src=entry.url; image.alt=entry.file.name;
    const remove=h('button','', '×'); remove.type='button'; remove.title='Remove '+entry.file.name;
    remove.onclick=() => { URL.revokeObjectURL(entry.url); imageFiles.splice(index,1); renderImages(); };
    card.append(image, remove, h('span','',entry.file.name)); list.appendChild(card);
  });
  show('image-list', imageFiles.length>0);
}

function addImages(files){
  const error=$('starterr');
  const accepted=[];
  for(const file of files){
    if(imageFiles.length+accepted.length>=6){ error.textContent='You can attach up to 6 images.'; break; }
    if(file.size>10*1024*1024){ error.textContent=file.name+' is over the 10 MB image limit.'; continue; }
    accepted.push({file:file,url:URL.createObjectURL(file)});
  }
  imageFiles=imageFiles.concat(accepted); renderImages();
}

function readImage(file){
  return new Promise((resolve,reject) => {
    const reader=new FileReader();
    reader.onerror=() => reject(new Error('Could not read '+file.name+'.'));
    reader.onload=() => {
      const value=String(reader.result||'');
      resolve({name:file.name,type:file.type,data:value.slice(value.indexOf(',')+1)});
    };
    reader.readAsDataURL(file);
  });
}

$('images').onchange = e => { addImages([...e.target.files]); e.target.value=''; };
$('voice').onchange = e => {
  voiceOn=e.target.checked;
  if(!voiceOn) stopSpeaking();
  else if('speechSynthesis' in window) window.speechSynthesis.resume();
};
if(!('speechSynthesis' in window)){
  $('voice').disabled=true; $('voice').title='Speech is not supported by this browser.';
}
$('btn-start').onclick = async () => {
  $('starterr').textContent='';
  const button=$('btn-start'); button.disabled=true;
  try{
    if(voiceOn && 'speechSynthesis' in window) window.speechSynthesis.resume();
    const images=await Promise.all(imageFiles.map(entry=>readImage(entry.file)));
    const r = await api('/api/start',{
      topic:$('topic').value,
      images:images,
      style:$('style').value,
      focus:document.querySelector('input[name="focus"]:checked').value,
      max_turns:+$('turns').value,
      first:$('first').value
    });
    if(!r.ok) $('starterr').textContent = r.error;
    else {
      $('topic').value='';
      imageFiles.forEach(entry=>URL.revokeObjectURL(entry.url)); imageFiles=[]; renderImages();
    }
  }catch(e){ $('starterr').textContent=e.message || 'Could not prepare the attachments.'; }
  finally{ button.disabled=false; }
};
$('btn-note').onclick = async () => {
  const t=$('note').value.trim(); if(!t) return;
  const r = await api('/api/note',{text:t}); if(r.ok) $('note').value='';
};
$('note').addEventListener('keydown', e => { if(e.key==='Enter') $('btn-note').click(); });
$('topic').addEventListener('keydown', e => { if(e.key==='Enter' && (e.ctrlKey||e.metaKey)) $('btn-start').click(); });
$('btn-stop').onclick = () => api('/api/stop');
$('source-file').onchange = e => {
  const f = e.target.files[0]; if(!f) return;
  const rd = new FileReader();
  rd.onload = () => { const t=$('topic'); t.value = (t.value ? t.value+'\n\n' : '') + '--- '+f.name+' ---\n'+rd.result; };
  rd.readAsText(f); e.target.value='';
};
poll();
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------- main


def main():
    global MOCK, SANDBOX
    ap = argparse.ArgumentParser(description="Agora: two LLM CLIs reasoning to a sealed joint statement.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--mock", action="store_true", help="simulated seats, no CLI calls")
    args = ap.parse_args()
    MOCK = args.mock
    STATE["mock"] = MOCK
    SANDBOX = Path(tempfile.mkdtemp(prefix="agora_sandbox_"))

    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("No free port near %d." % args.port)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Agora open at %s%s   (Ctrl+C to close)" % (url, "  [simulation]" if MOCK else ""))
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        kill_all()
        server.server_close()
        shutil.rmtree(SANDBOX, ignore_errors=True)
        print("Agora closed.")


if __name__ == "__main__":
    main()
