# leetcode-daily

Solves 2 random LeetCode problems per day with a local open-source model (`qwen3:8b` via [Ollama](https://ollama.com)), starting automatically when you log in. Nothing is sent to a paid API.

Each run picks random problem numbers and skips any you've already solved (and premium-only ones unless you have Premium). For each remaining problem it:

1. Looks up a published solution in the public GitHub collections [doocs/leetcode](https://github.com/doocs/leetcode) (CC-BY-SA 4.0) and [walkccc/LeetCode](https://github.com/walkccc/LeetCode) (MIT). These cover nearly every problem except the newest. If one is found, it runs against the examples as-is and is submitted if it passes.
2. Otherwise, or if that solution fails, the local model writes one. It sees the online solution and the judge's verdict on it, if there was one.
3. When a run or submission fails, the judge's output goes back to the model for another try (4 attempts per problem by default).

Accepted solutions are saved to `~/.local/state/leetcode-daily/solutions/`, each with a comment noting where it came from. Set `ONLINE_LOOKUP=false` in the config to always use the model.

By default it only picks easy and medium problems, because a 4B-8B model rarely solves hard ones.

## Requirements

- Ollama running as a system service (`systemctl is-enabled ollama`) with the model pulled:
  ```bash
  ollama pull qwen3:8b
  ```
  `qwen3:8b` needs about 7 GB of VRAM with the context size used here, which fits an 8 GB GPU. On a smaller GPU, use `qwen3:4b` and set `MODEL=qwen3:4b` in the config.
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
./install.sh
```

Then edit `~/.config/leetcode-daily/config.env` and set `LEETCODE_SESSION` and `LEETCODE_CSRFTOKEN`. These are cookies copied from your browser while logged in to leetcode.com; the file explains where to find them. LeetCode puts a CAPTCHA on password logins, so the program reuses your browser session instead.

Check that the login and the model work (no submissions):

```bash
uv run --script ~/.local/share/leetcode-daily/leetcode_daily.py --check
```

Optionally, solve problems and test them against the examples without submitting anything:

```bash
uv run --script ~/.local/share/leetcode-daily/leetcode_daily.py --dry-run
```

## How it runs

A systemd user timer starts the program 2 minutes after login and then every hour. Once 2 problems are accepted for the day, each run exits right away. If the network or Ollama wasn't ready, or a run failed, the next hourly run picks up where it left off. You get a desktop notification when problems are solved, or when the cookies expire and need to be copied again.

On an RTX 4060 laptop GPU, one answer takes about a minute.

```bash
systemctl --user list-timers leetcode-daily.timer    # next run
journalctl --user -u leetcode-daily -e               # logs (also in ~/.local/state/leetcode-daily/log.txt)
systemctl --user start leetcode-daily                # run now
uv run --script ~/.local/share/leetcode-daily/leetcode_daily.py --force   # solve 2 more today
```

Settings in `config.env`: `ONLINE_LOOKUP`, `MODEL`, `LANGUAGE` (LeetCode slug: `python3`, `cpp`, `java`, ...), `DAILY_COUNT`, `DIFFICULTY`, `MAX_ATTEMPTS`, and `MAX_TRIES_PER_DAY`. `MAX_TRIES_PER_DAY` limits how many problems are attempted per day, failures included.

After editing `leetcode_daily.py`, run `./install.sh` again to install the new copy. The program runs from `~/.local/share/leetcode-daily`, not from this folder, because this drive may not be mounted yet when you log in.

## Uninstall

```bash
systemctl --user disable --now leetcode-daily.timer
rm ~/.config/systemd/user/leetcode-daily.{service,timer}
rm -r ~/.local/share/leetcode-daily ~/.config/leetcode-daily ~/.local/state/leetcode-daily
ollama rm qwen3:8b
```

## Caveats

- Automated submissions are likely against LeetCode's terms of service, and the account could be flagged.
- Solved problems count toward your profile stats even though you didn't write the solutions.
