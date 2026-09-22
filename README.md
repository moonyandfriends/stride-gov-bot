# stride-gov-bot

Watches Stride governance and posts every new proposal to **Slack**, **Discord**, and **push**
(ntfy and/or Pushover), tagging you on Slack and Discord. Each alert includes:

- **Title**, **proposal type** (such as `MsgSoftwareUpgrade (cosmos.upgrade)` or
  `Text / signaling`), status, voting/deposit deadline, and proposer
- **Proposal text** (the on-chain summary or description)
- **On-chain contents**: every message decoded field by field (upgrade plan and height, spend
  recipient and amounts, params, IBC client IDs, and so on). The gov module address is labelled.
- **AI review** (optional, OpenAI): a plain-English summary, what passing would change,
  a risk level (🟢 low / 🟡 medium / 🟠 high / 🔴 critical), specific concerns, and a
  one-line verdict. High/critical risk sends an *urgent* push.

Python standard library only; no dependencies.

## Deploy on Railway

1. **New Project → Deploy from GitHub repo →** pick this repo. It builds from the `Dockerfile`.
2. **Add a volume** to the service, mounted at **`/data`**. This stores the last-seen proposal
   ID. Without it, a proposal submitted while the bot is redeploying could be missed.
3. **Variables:** add the ones you need from `config.env.example` (`SLACK_WEBHOOK_URL`,
   `SLACK_USER_ID`, `DISCORD_WEBHOOK_URL`, `DISCORD_USER_ID`, `NTFY_URL` or `PUSHOVER_*`,
   `OPENAI_API_KEY`).
4. Deploy. The logs should show `channels: slack, discord, ntfy | ... | AI review: gpt-6-astra`
   and then `initialized at proposal #N`.

Keep it at **one replica**. Two replicas would each send every alert.

To send a test alert from Railway, run `python gov_bot.py --test` as a one-off command (or
`railway run python gov_bot.py --test` locally with the service linked).

## Run locally

    cp config.env.example config.env        # fill in
    ./gov_bot.py --preview 275              # print what would be posted (runs the AI review), sends nothing
    ./gov_bot.py --test                     # send a [TEST] alert for the latest proposal
    ./gov_bot.py                            # run the poll loop

`--announce ID` re-posts a specific proposal to every channel.

## Behaviour

- Polls `/cosmos/gov/v1/proposals` every `POLL_SECONDS` (default 60), falling back through
  `STRIDE_LCD_ENDPOINTS`.
- On first run it saves the current highest proposal ID and doesn't post old proposals.
- The AI review runs **once** per proposal and is cached. If a channel fails, only that channel
  retries on the next poll, with the same review, so nothing gets sent twice.
- If the OpenAI call fails, the alert still goes out, marked "AI review unavailable".

## Safety notes

- Proposal text is written by whoever submitted it. The bot escapes it for Slack (so it can't
  create `@channel` pings or disguised links) and locks Discord `allowed_mentions` to your user ID.
- The AI is told to treat the proposal as untrusted data and to flag any attempt to steer it.
  A proposal can still try to fool the model, so **the AI verdict is advisory only**. The decoded
  on-chain contents in the same alert are what actually executes.

## Tests

    python3 -m unittest discover -s tests -t tests -v
