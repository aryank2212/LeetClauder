#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31"]
# ///
"""Solve a few random, unsolved LeetCode problems per day with a local LLM (Ollama).

Each run:
  1. signs in to LeetCode with your browser session cookies,
  2. picks random problem numbers and skips any you've already solved,
  3. looks for a published solution online and tries it first,
  4. otherwise (or if that fails) asks the local model, runs its code against
     the examples, then submits,
  5. feeds failures back to the model and retries (up to MAX_ATTEMPTS),
  6. stops once DAILY_COUNT problems are accepted today.

Runs after today's quota is met exit immediately, so it is safe to run on
every login and on an hourly timer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

APP = "leetcode-daily"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP / "config.env"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "log.txt"
SOLUTIONS_DIR = STATE_DIR / "solutions"

BASE = "https://leetcode.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

DEFAULTS = {
    "LEETCODE_SESSION": "",
    "LEETCODE_CSRFTOKEN": "",
    "OLLAMA_URL": "http://localhost:11434",
    "MODEL": "qwen3:8b",
    "LANGUAGE": "python3",
    "DAILY_COUNT": "2",
    "MAX_ATTEMPTS": "4",
    "MAX_TRIES_PER_DAY": "10",
    "DIFFICULTY": "easy,medium",
    "ONLINE_LOOKUP": "true",
}

# Qwen3's recommended sampling for thinking mode. num_ctx is sized so qwen3:8b
# plus its context cache fits in 8 GB of VRAM; num_predict stops runaway thinking.
MODEL_OPTIONS = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "num_ctx": 12288, "num_predict": 8192}
GENERATION_TIMEOUT = 900  # seconds per answer

FILE_EXT = {
    "python3": "py", "python": "py", "cpp": "cpp", "java": "java", "c": "c",
    "csharp": "cs", "javascript": "js", "typescript": "ts", "golang": "go",
    "rust": "rs", "kotlin": "kt", "swift": "swift", "ruby": "rb", "scala": "scala",
    "php": "php", "dart": "dart",
}

DIFFICULTY_LEVELS = {"easy": 1, "medium": 2, "hard": 3}
DIFFICULTY_NAMES = {1: "Easy", 2: "Medium", 3: "Hard"}


class LeetCodeError(Exception):
    pass


class SessionExpired(LeetCodeError):
    pass


class SolverUnavailable(Exception):
    """The local model can't be reached or isn't installed; stop this run."""


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "LeetCode Daily", title, body], check=False)


def warn_once(state: dict, title: str, body: str) -> None:
    """Desktop warning at most once a day, so the hourly retries don't spam."""
    if state.get("notified") != state["date"]:
        notify(title, body)
        state["notified"] = state["date"]
        save_state(state)


def load_config(path: Path) -> dict[str, str]:
    cfg = dict(DEFAULTS)
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip("\"'")
    # Environment variables override the file.
    for key in DEFAULTS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def load_state(today: str) -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "solved": [], "failed": [], "notified": state.get("notified")}
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def wait_for_network(timeout: int = 300) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            requests.head(BASE, timeout=10)
            return True
        except requests.RequestException:
            if time.monotonic() > deadline:
                return False
            time.sleep(15)


def html_to_text(content: str) -> str:
    text = re.sub(r"<sup>(.*?)</sup>", r"^\1", content, flags=re.S)
    text = re.sub(r"<li>", "- ", text)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</pre>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_code(text: str, starter: str = "") -> str | None:
    """The last fenced code block, preferring one that defines the starter code's class.

    Small models often follow the solution with an "example usage" block.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)
    if not blocks:
        return None
    if m := re.search(r"class\s+(\w+)", starter):
        with_class = [b for b in blocks if re.search(rf"class\s+{m.group(1)}\b", b)]
        blocks = with_class or blocks
    return blocks[-1].strip() + "\n"


# ----------------------------------------------------------------- LeetCode

class LeetCode:
    def __init__(self, session_cookie: str, csrftoken: str):
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": BASE + "/",
            "Origin": BASE,
            "x-csrftoken": csrftoken,
        })
        self.http.cookies.set("LEETCODE_SESSION", session_cookie, domain=".leetcode.com")
        self.http.cookies.set("csrftoken", csrftoken, domain=".leetcode.com")

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        r = self.http.post(f"{BASE}/graphql", json={"query": query, "variables": variables or {}}, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise LeetCodeError(body["errors"][0].get("message", body["errors"]))
        return body["data"]

    def user_status(self) -> dict:
        return self.graphql("{ userStatus { isSignedIn username isPremium } }")["userStatus"]

    def algorithm_problems(self) -> list[dict]:
        r = self.http.get(f"{BASE}/api/problems/algorithms/", timeout=60)
        r.raise_for_status()
        return r.json()["stat_status_pairs"]

    def question(self, slug: str) -> dict:
        query = """
        query question($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            questionId questionFrontendId title titleSlug difficulty status
            isPaidOnly content exampleTestcases sampleTestCase
            codeSnippets { langSlug code }
          }
        }"""
        return self.graphql(query, {"titleSlug": slug})["question"]

    def _post(self, slug: str, action: str, payload: dict) -> dict:
        url = f"{BASE}/problems/{slug}/{action}/"
        headers = {"Referer": f"{BASE}/problems/{slug}/"}
        for _ in range(6):
            r = self.http.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code == 429:  # "You have attempted to run code too soon"
                time.sleep(15)
                continue
            if r.status_code in (401, 403):
                raise SessionExpired(f"{action} returned HTTP {r.status_code}")
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                raise LeetCodeError(f"{action}: {body['error']}")
            return body
        raise LeetCodeError(f"{action}: still rate limited after several retries")

    def _wait_for_result(self, run_id: str, timeout: int = 180) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            r = self.http.get(f"{BASE}/submissions/detail/{run_id}/check/", timeout=30)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            result = r.json()
            if result.get("state") == "SUCCESS":
                return result
        raise LeetCodeError(f"judge did not finish {run_id} within {timeout}s")

    def run_examples(self, q: dict, lang: str, code: str, data_input: str) -> dict:
        payload = {"lang": lang, "question_id": q["questionId"], "typed_code": code, "data_input": data_input}
        return self._wait_for_result(self._post(q["titleSlug"], "interpret_solution", payload)["interpret_id"])

    def submit(self, q: dict, lang: str, code: str) -> dict:
        payload = {"lang": lang, "question_id": q["questionId"], "typed_code": code}
        return self._wait_for_result(str(self._post(q["titleSlug"], "submit", payload)["submission_id"]))


def error_text(result: dict) -> str | None:
    if result.get("full_compile_error") or result.get("compile_error"):
        return "Compile Error:\n" + (result.get("full_compile_error") or result["compile_error"])
    if result.get("full_runtime_error") or result.get("runtime_error"):
        return "Runtime Error:\n" + (result.get("full_runtime_error") or result["runtime_error"])
    return None


def example_feedback(result: dict, data_input: str) -> str | None:
    """None if the code passed the example tests, otherwise a description of what went wrong."""
    if err := error_text(result):
        return f"{err}\nInput:\n{data_input}"
    if not result.get("run_success", True):
        return f"{result.get('status_msg', 'Failed')} on the example tests.\nInput:\n{data_input}"
    if "correct_answer" in result:
        passed = result["correct_answer"]
    elif "expected_code_answer" in result:
        passed = result.get("code_answer") == result["expected_code_answer"]
    else:
        passed = True  # can't tell from this response; the real submission will decide
    if passed:
        return None
    return (
        f"Wrong Answer on the example tests.\nInput:\n{data_input}\n"
        f"Your output: {result.get('code_answer')}\nExpected: {result.get('expected_code_answer')}"
    )


def submission_feedback(result: dict) -> str | None:
    """None if accepted, otherwise a description of the failing case."""
    if result.get("status_code") == 10 or result.get("status_msg") == "Accepted":
        return None
    passed = f"{result.get('total_correct')}/{result.get('total_testcases')} tests passed"
    parts = [f"{result.get('status_msg', 'Rejected')} ({passed})."]
    if err := error_text(result):
        parts.append(err)
    if result.get("last_testcase"):
        parts.append(f"Failing input:\n{result['last_testcase']}")
    if result.get("expected_output"):
        parts.append(f"Expected: {result['expected_output']}")
    if result.get("code_output"):
        parts.append(f"Your output: {result['code_output']}")
    return "\n".join(parts)


# -------------------------------------------------------------- local model

SYSTEM_PROMPT = (
    "You are an expert competitive programmer solving LeetCode problems. "
    "Write a correct solution that fits within LeetCode's time and memory limits. "
    "Keep the class name and method signatures from the starter code exactly as given, "
    "and only use the language's standard library. "
    "Do not read input, print, or add example usage or tests. "
    "Reply with one fenced code block containing the complete solution."
)


class Solver:
    """Writes solutions with a model served by Ollama."""

    def __init__(self, cfg: dict[str, str]):
        self.url = cfg["OLLAMA_URL"].rstrip("/")
        self.model = cfg["MODEL"]
        self.think = True  # switched off automatically for models without a reasoning mode
        self.ran_out_thinking = False  # set when the last answer hit num_predict before writing any code

    def check(self, wait: int = 120) -> None:
        """Wait for Ollama to come up (it may still be starting after login) and confirm the model is pulled."""
        deadline = time.monotonic() + wait
        while True:
            try:
                r = requests.get(f"{self.url}/api/tags", timeout=10)
                r.raise_for_status()
                break
            except requests.RequestException as e:
                if time.monotonic() > deadline:
                    raise SolverUnavailable(f"Ollama isn't reachable at {self.url}: {e}") from e
                time.sleep(10)
        names = {m["name"] for m in r.json().get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise SolverUnavailable(f"Model {self.model} isn't downloaded. Run: ollama pull {self.model}")

    def write_solution(self, q: dict, lang: str, starter: str, attempts: list[tuple[str, str, str]],
                       think: bool = True) -> str | None:
        """attempts: (label, code, judge feedback) for each earlier try at this problem."""
        self.ran_out_thinking = False
        prompt = (
            f"Problem {q['questionFrontendId']}. {q['title']} ({q['difficulty']})\n\n"
            f"{html_to_text(q['content'] or '')}\n\n"
            f"Language: {lang}\nStarter code:\n```{lang}\n{starter}\n```"
        )
        for label, code, feedback in attempts:
            prompt += (
                f"\n\n{label}:\n```{lang}\n{code}```\n"
                f"LeetCode's judge result:\n{feedback}"
            )
        if attempts:
            prompt += "\n\nFind the bug or inefficiency and write a corrected solution."

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "think": self.think and think,
            "stream": False,
            "options": MODEL_OPTIONS,
        }
        started = time.monotonic()
        try:
            r = requests.post(f"{self.url}/api/chat", json=payload, timeout=(10, GENERATION_TIMEOUT))
            if payload["think"] and r.status_code != 200 and "does not support thinking" in r.text:
                log(f"    {self.model} has no reasoning mode; asking without it")
                self.think = payload["think"] = False
                r = requests.post(f"{self.url}/api/chat", json=payload, timeout=(10, GENERATION_TIMEOUT))
        except requests.Timeout:
            log(f"    model took longer than {GENERATION_TIMEOUT}s")
            return None
        except requests.ConnectionError as e:
            raise SolverUnavailable(f"Ollama isn't reachable at {self.url}") from e
        if r.status_code != 200:
            raise SolverUnavailable(f"Ollama error {r.status_code}: {r.text[:200]}")

        body = r.json()
        log(f"    model answered in {time.monotonic() - started:.0f}s ({body.get('eval_count', '?')} tokens)")
        if body.get("done_reason") == "length" and not body["message"].get("content"):
            log("    model ran out of tokens while thinking")
            self.ran_out_thinking = True
            return None
        return extract_code(body["message"].get("content", ""), starter)


# --------------------------------------------------------------------- main

def find_online_solution(num: int, title: str, lang: str) -> tuple[str, str] | None:
    """Look the problem up in public GitHub solution collections. Returns (url, code) or None."""
    ext = FILE_EXT.get(lang)
    if not ext:
        return None
    lo = num // 100 * 100
    urls = [
        # doocs/leetcode (CC-BY-SA 4.0): widest coverage, including recent problems
        f"https://raw.githubusercontent.com/doocs/leetcode/main/solution/{lo:04d}-{lo + 99:04d}/"
        f"{quote(f'{num:04d}.{title}')}/Solution.{ext}",
        # walkccc/LeetCode (MIT)
        f"https://raw.githubusercontent.com/walkccc/LeetCode/main/solutions/{quote(f'{num}. {title}')}/{num}.{ext}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
        except requests.RequestException:
            continue
        if r.ok and r.text.strip():
            return url, r.text.strip() + "\n"
    return None


def judge(lc: LeetCode, q: dict, lang: str, code: str, dry_run: bool) -> str | None:
    """Run the examples, then submit. None if accepted (or passed the examples in a dry run), else the feedback."""
    data_input = q.get("exampleTestcases") or q.get("sampleTestCase") or ""
    if data_input:
        feedback = example_feedback(lc.run_examples(q, lang, code, data_input), data_input)
        if feedback:
            log(f"    failed examples: {feedback.splitlines()[0]}")
            return feedback
        log("    passed example tests")

    if dry_run:
        log("    dry run: not submitting")
        return None

    result = lc.submit(q, lang, code)
    feedback = submission_feedback(result)
    if feedback is None:
        log(f"    Accepted ({result.get('status_runtime', '?')}, {result.get('status_memory', '?')})")
    else:
        log(f"    submission rejected: {feedback.splitlines()[0]}")
    return feedback


def solve(lc: LeetCode, solver: Solver, q: dict, lang: str, starter: str,
          max_attempts: int, dry_run: bool, lookup: bool) -> tuple[str, str] | None:
    """Return (accepted code, where it came from), or None if every attempt failed.

    A solution published online is tried first, as-is. If it's missing or fails, the
    model takes over, seeing the online code and the judge's verdict on it.
    """
    attempts: list[tuple[str, str, str]] = []
    if lookup:
        found = find_online_solution(int(q["questionFrontendId"]), q["title"], lang)
        if found is None:
            log("    no published solution found online")
        else:
            url, code = found
            log(f"    trying the solution published at {url}")
            feedback = judge(lc, q, lang, code, dry_run)
            if feedback is None:
                return code, url
            attempts.append(("A solution found online (it may need adapting to the starter code)", code, feedback))

    think = True
    for attempt in range(1, max_attempts + 1):
        log(f"    attempt {attempt}/{max_attempts}: asking {solver.model}{'' if think else ' (no reasoning)'}")
        code = solver.write_solution(q, lang, starter, attempts, think=think)
        if solver.ran_out_thinking:
            # Reasoning went in circles; ask for code directly for the rest of this problem.
            think = False
        if not code:
            log("    no code in the model's reply")
            continue
        feedback = judge(lc, q, lang, code, dry_run)
        if feedback is None:
            return code, f"{solver.model} (local model)"
        attempts.append(("A previous attempt", code, feedback))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"config file (default {CONFIG_PATH})")
    parser.add_argument("--check", action="store_true", help="verify LeetCode login and the local model, then exit")
    parser.add_argument("--dry-run", action="store_true", help="solve and test against examples, but don't submit")
    parser.add_argument("--force", action="store_true", help="run even if today's quota is already met")
    args = parser.parse_args()

    cfg = load_config(args.config)
    today = dt.date.today().isoformat()
    state = load_state(today)
    daily = int(cfg["DAILY_COUNT"])
    lang = cfg["LANGUAGE"]

    if not args.check and not args.force and len(state["solved"]) >= daily:
        print(f"Already solved {daily} today, nothing to do.")
        return 0

    if not cfg["LEETCODE_SESSION"] or not cfg["LEETCODE_CSRFTOKEN"]:
        log(f"LEETCODE_SESSION / LEETCODE_CSRFTOKEN missing in {args.config}")
        warn_once(state, "LeetCode Daily needs setup", f"Add your LeetCode cookies to {args.config}")
        return 2

    if not wait_for_network():
        log("No network connection to leetcode.com; will retry on the next run")
        return 1

    lc = LeetCode(cfg["LEETCODE_SESSION"], cfg["LEETCODE_CSRFTOKEN"])
    user = lc.user_status()
    if not user["isSignedIn"]:
        log("LeetCode session cookie is expired or invalid")
        warn_once(state, "LeetCode Daily: login expired",
                  f"Copy fresh LEETCODE_SESSION and csrftoken cookies into {args.config}")
        return 2
    log(f"Signed in to LeetCode as {user['username']} (premium: {bool(user['isPremium'])})")

    try:
        solver = Solver(cfg)
        solver.check()
    except SolverUnavailable as e:
        log(str(e))
        warn_once(state, "LeetCode Daily: model unavailable", str(e))
        return 2
    if args.check:
        log(f"Model {solver.model} is ready in Ollama. Setup looks good.")
        return 0

    wanted = {DIFFICULTY_LEVELS[d.strip().lower()] for d in cfg["DIFFICULTY"].split(",") if d.strip()}
    pool = [
        p for p in lc.algorithm_problems()
        if (user["isPremium"] or not p["paid_only"])
        and (not wanted or p["difficulty"]["level"] in wanted)
    ]
    random.shuffle(pool)
    done_slugs = {s["slug"] for s in state["solved"]} | set(state["failed"])
    target = len(state["solved"]) + daily if args.force else daily
    max_tries = int(cfg["MAX_TRIES_PER_DAY"]) + (len(state["solved"]) if args.force else 0)
    dry_run_tries = 0
    newly_solved = []

    for p in pool:
        if len(state["solved"]) >= target:
            break
        if len(state["solved"]) + len(state["failed"]) >= max_tries:
            log(f"Reached MAX_TRIES_PER_DAY ({cfg['MAX_TRIES_PER_DAY']}); trying again tomorrow")
            break
        if args.dry_run and dry_run_tries >= daily:
            break
        num, title, slug = (p["stat"]["frontend_question_id"], p["stat"]["question__title"],
                            p["stat"]["question__title_slug"])
        if slug in done_slugs:
            continue
        log(f"Random pick: #{num} {title} [{DIFFICULTY_NAMES[p['difficulty']['level']]}]")
        if p["status"] == "ac":
            log("    already solved, picking another")
            continue

        attempted = False
        try:
            q = lc.question(slug)
            if q["status"] == "ac":
                log("    already solved, picking another")
                continue
            starter = next((s["code"] for s in q["codeSnippets"] or [] if s["langSlug"] == lang), None)
            if starter is None:
                log(f"    no {lang} starter code for this problem, skipping")
                continue
            attempted = True
            lookup = cfg["ONLINE_LOOKUP"].lower() in ("1", "true", "yes", "on")
            solved = solve(lc, solver, q, lang, starter, int(cfg["MAX_ATTEMPTS"]), args.dry_run, lookup)
        except SolverUnavailable as e:
            log(f"Stopping: {e}")
            warn_once(state, "LeetCode Daily: model unavailable", str(e))
            return 1
        except SessionExpired as e:
            log(f"Stopping: LeetCode session rejected ({e})")
            warn_once(state, "LeetCode Daily: login expired", f"Copy fresh cookies into {args.config}")
            return 2
        except (LeetCodeError, requests.RequestException) as e:
            log(f"    LeetCode error, skipping: {e}")
            solved = None
            if not attempted:
                continue

        if args.dry_run:
            dry_run_tries += 1
            continue
        if solved is None:
            log("    gave up on this problem")
            state["failed"].append(slug)
        else:
            code, source = solved
            comment = "#" if lang in ("python3", "python", "ruby") else "//"
            SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
            path = SOLUTIONS_DIR / f"{int(num):04d}-{slug}.{FILE_EXT.get(lang, 'txt')}"
            path.write_text(f"{comment} Source: {source}\n{code}")
            state["solved"].append({"id": num, "title": title, "slug": slug, "solution": str(path), "source": source})
            newly_solved.append(f"#{num} {title}")
        save_state(state)

    if newly_solved:
        notify(f"LeetCode Daily: solved {len(newly_solved)}", "\n".join(newly_solved))
    log(f"Done. Solved today: {len(state['solved'])}/{daily}")
    return 0 if args.dry_run or len(state["solved"]) >= daily else 1


if __name__ == "__main__":
    sys.exit(main())
