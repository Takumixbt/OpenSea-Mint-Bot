# OpenSea Mint Bot Radar

Finds free mints before they are public, screens them for scams, ranks them into
a Notion watchlist, and mints the ones you authorize.

The original bot in this folder was an executor with no eyes: you hand-typed a
collection slug into `config.py` and it raced the clock. This is the half that
decides *what* to race for, which is where the money actually is.

---

## Why this is built the way it is

**Speed does not win a free mint.** On a first-come-first-served drop, thousands
of bots land in the same block. Shaving 200ms off your send does close to
nothing. *Knowing earlier* wins, and the earliest public trace a project leaves
is that people with good taste start following its X account, days before any
mint is announced.

So the core of this system is a **follow-graph diff**. X gives no "followed at"
timestamp and `twitter-cli` only returns snapshots, so the timing is recovered
by snapshotting who your curated smart accounts follow, then diffing each sweep
against the last. A handle newly followed by several of them is a candidate
before it is news.

**The same list is also the search engine.** X's search endpoint is dead from
`twitter-cli`'s session, so instead of searching all of X for "free mint" the
radar reads the curated accounts' own timelines. That is the better instrument
anyway: a search returns every chain's noise, while a timeline hit already
carries the endorsement of someone whose taste put them on the list. Retweets
are the best shape of all, because X reports the original author separately
from the amplifier, which hands over the project's handle directly.

**The screen rejects scams, not mediocrity.** Gas on Robinhood Chain is near
zero, so minting a dud costs cents while missing a real one costs multiple ETH.
That asymmetry makes a precision filter a losing trade. The screen only blocks
things that look actively malicious; everything else lands on the board with a
risk flag and a lower rank.

**The last source is the chain itself.** ERC-721 transfers out of the zero
address are the definition of a mint. X can 404 and OpenSea can start demanding
an API key (both have, during this build), but that feed cannot be suppressed,
delayed, or rate-limited. It is the latest of the three signals, since by then
the mint is already open, but it is the only one that is never wrong, and it is
the only one that sees the projects which never announce on X at all. On a chain
where drops run for minutes rather than seconds, arriving during the mint is
still arriving in time.

So the three sources ladder by earliness and by reliability, in opposite
directions, which is the point of running all three:

| Source | Timing | Fails when |
|---|---|---|
| Follow graph | days early | the list goes stale |
| Smart timelines | hours early | nobody posts |
| On-chain mints | live now | never |

**Minting works by replaying a real mint, not by guessing.** The old OpenSea
path needed a working SIWE login and a server-signed calldata blob, both of
which break without notice (the login is broken right now). The obvious
replacement is to guess at `mint(uint256)` and friends, and that turns out not
to work either: a survey of 60 real mints on this chain found not one of the
guessable signatures, and four of the selectors actually in use resolve to no
published signature at all.

So the executor copies instead. It takes a transaction that just successfully
minted the collection, substitutes your address for the original minter's, and
simulates it. If the simulation passes, the chain has already proven that exact
call mints, and it goes out. This works on arbitrary contract shapes, and it
handles calls routed elsewhere: OpenSea's SeaDrop mints are sent to SeaDrop with
the collection as an argument, so the correct destination travels with the
calldata rather than being assumed.

It cannot replay a mint gated on a server signature or a per-address Merkle
proof, since those encode the original minter. Neither can anyone else, so
nothing is lost. Guess-based probing is still tried as a fallback.

---

## The pieces

| File | Job |
|---|---|
| `radar/smart_graph.py` | The edge. Snapshots and diffs the follow graph. |
| `radar/discover.py` | The wide net. Smart-account timelines, the on-chain mint feed, OpenSea. |
| `radar/osint.py` | The safety screen. X account age, deployer wallet history. |
| `radar/chain.py` | Raw Robinhood Chain RPC. Deployer resolution by binary search. |
| `radar/mint_direct.py` | Replays a proven mint call. Guess-probing as fallback. |
| `radar/score.py` | Ranking, so your attention goes to the right row first. |
| `radar/notion_log.py` | The watchlist, and the control surface you arm rows on. |

Entry points: `radar_bootstrap.py`, `radar_scan.py`, `radar_watch.py`.

**Watchlist:** https://app.notion.com/p/b590585246864b7999cbe767e7031853

---

## Setup

### 1. Nothing, actually

The system runs with zero configuration beyond the `.env` the mint bot already
needed. Notion is optional; see step 4.

### 2. The smart-account list

`radar/smart_accounts.txt` is already populated with 23 hand-picked NFT/CT
accounts. That file is the whole edge, so it deserves maintenance:

- **Prune** anyone who follows hundreds of accounts a week. They generate noise
  that drowns out the rest of the list.
- **Re-seed** after every mint you win or miss, adding whoever was early.

`radar_bootstrap.py` can rebuild the list empirically from the followers of a
proven winner, which is a better prior than any hand-picked list. It is on hold:
X's follower endpoint 404s right now, so it falls back to a weaker signal. The
hand-picked list is the better source until that endpoint returns.

### 3. Lay the baseline

```
python radar_scan.py
```

The first sweep produces **no candidates on purpose**. It records what everyone
currently follows. Treating an entire existing following list as "new follows"
would bury the real signal. The signal appears on the second sweep.

### 4. Sweep on a schedule

Run `python radar_scan.py` every few hours. Each sweep:

1. diffs the follow graph,
2. widens with the smart-account timelines, the on-chain mint feed, and OpenSea,
3. screens each candidate (X account age, contract bytecode, deployer history),
4. scores and ranks,
5. writes the watchlist.

A sweep takes roughly 17 minutes with 23 accounts: two throttled passes over the
list at `XCLI_PAUSE_SECONDS` each. That throttle is not negotiable, see Tuning.
`--skip-graph` drops it to about 7 by skipping the follow pass, which is useful
for a quick look but throws away the leading signal, so do not schedule it.

Schedule it every three hours (Windows):

```
schtasks /create /tn "FreeMintRadar" /sc hourly /mo 3 /tr ^
  "cmd /c cd /d C:\Users\Takum\free-mint-bot && python radar_scan.py >> radar\state\sweep.log 2>&1"
```

Three hours is a deliberate choice. The follow window is capped at 200 entries
per account (see Known limits), so sweeping too rarely risks a new follow
scrolling out of view before it is ever seen, while sweeping much more often
buys little and spends your rate limit.

**Connecting Notion (optional).** Add a `NOTION_TOKEN` to `.env` from
notion.so/my-integrations, then **share the database with the integration**
(open the watchlist, `...` menu, Connections, Connect to, pick it). The share
step is required; the token alone cannot see the database.

Without it nothing is lost. The board is mirrored to
`radar/state/board.json` and rows are queued to `notion_queue.jsonl`;
`python radar_scan.py --flush` pushes everything once the token lands.

### 5. Arm what you want

Rows arrive sorted by score with their risk flags and OSINT notes. Authorize one
by ticking **Armed** in Notion, or by setting `"armed": true` on its row in
`radar/state/board.json`. The executor treats the two identically.

Nothing in the code ever arms a row. A high score does not authorize spending,
and `score.status_for()` is written so it cannot return "Armed" at all. A
re-scan preserves whatever you set. That tick is the whole safety model.

### 6. Leave the executor running

```
python radar_watch.py --dry-run     # rehearsal, never broadcasts
python radar_watch.py               # live
```

It polls the watchlist, and for each armed row probes the contract in a tight
loop as the open time arrives. Before a mint opens every call reverts, which is
exactly how it knows the mint is not live yet. The first call that simulates
successfully means it just opened, and the transaction goes out on that same
iteration. Results are written back to the row.

`MAX_MINT_PRICE_NATIVE` in `config.py` is a hard spending cap and defaults to
`"0"`. A replayed call carries the original transaction's native-coin value,
so this is what stops a paid mint going out when a free one was expected. Raise
it only deliberately.

**Rehearse with `--dry-run` first.** Do not let the first live run be on a drop
you care about.

---

## Tuning

Everything lives in `radar/settings.py`:

- `MIN_SMART_FOLLOWS` (default 2) is the main dial. Higher means later but more
  certain. The asymmetric payoff argues for keeping it low.
- `W_SMART_FOLLOW` dominates scoring on purpose. It is the only genuinely
  leading signal; engagement and announcements all lag the crowd.
- `W_SMART_MENTION` sits well below it deliberately. Posting about a project is
  a public act the whole timeline sees; quietly following it is what happens
  before anyone is looking.
- `TIMELINE_POSTS_PER_ACCOUNT` and `MINT_KEYWORDS` control the timeline scan.
  The keyword list is loose on purpose, because the safety screen is the real
  filter and a missed mint costs far more than a screened dud.
- `BLOCKING_FLAGS` is the only thing that rejects outright. Keep it short and
  genuinely fatal.
- `XCLI_PAUSE_SECONDS` (default 3s) throttles the sweep. Do not lower it, and
  never run the follow-graph sweep from a VPS or datacenter IP. X limits
  follower endpoints from datacenter ranges aggressively, and a limited account
  costs you the whole signal.

## Being mentioned is not being the mint

The timeline scan pulls every handle out of a matching post, and those handles
are not equally meaningful. A smart account **retweeting** a project's own post
is real endorsement. A handle merely **@-mentioned** in someone's text also
covers the artist, the collab, the friend, and whoever got tagged for reach.

Weighting those the same put a personal account with no mint, no contract, and
no price at the top of the board, above collections that were demonstrably
minting for free at that moment. `EVIDENCE_MULTIPLIER` now discounts by shape
(retweeted 1.0, quoted 0.6, mentioned 0.25), and `W_NO_CONTRACT` (-45) applies
to any row with no contract at all, since without one there is no price, no
deployer, no bytecode, and no proof it is a mint rather than a person.

The effect on that row: 74.8 down to 3.0, and a confirmed live free mint took
the top. Social chatter stays on the board because it may resolve into
something later, but it can no longer outrank on-chain fact.

## "Free mint" is a claim, not a fact

Most drops on this chain that call themselves free mints are not free. Measured
across the board on 2026-08-05: Broker Senders charged 0.0065 to 0.013 ETH,
StonkBoys 0.0002 to 0.0019, and several others showed a spread starting at zero,
which means tiered pricing rather than a free stage for everyone.

So the radar does not take the word "free" from anyone. For every row with a
contract it reads what people have **actually paid** in recent transactions and
writes it to the **Mint Price** column. Anything above zero gets a `paid-mint`
flag and `W_PAID_MINT` (-60), which drops it far down the board without hiding
it, since a cheap mint may still be worth having.

Two independent guards, deliberately:

1. The board tells you the price before you arm anything.
2. `MAX_MINT_PRICE_NATIVE` in `config.py` (default `"0"`) makes the executor
   refuse to send a paid transaction even if you arm one by mistake.

## Every row carries its sources

The **Sources** column holds the links needed to check a row by hand: the X
posts it was found in, the project's X account, the contract and deployer on the
explorer, and the OpenSea collection page. A row that cannot be verified is a
row you cannot act on, so the score tells you how interesting something is and
these tell you why.

## Known limits

- **The follow graph is only as good as the list.** A list of celebrities who
  follow 5,000 accounts each produces noise. Prune high-volume followers.
- **Two of four X endpoints are dead.** Verified 2026-08-05 against the current
  `twitter-cli` session:

  | Endpoint | State | Consequence |
  |---|---|---|
  | `following` | works | follow-graph diff is fine |
  | `user` (profile) | works | account-age screening is fine |
  | `user-posts` | works | timeline scan is fine |
  | `search` | 404 | replaced by the timeline scan |
  | `followers` | 404 | `radar_bootstrap.py` degrades to a weaker signal |

  Every X call degrades to a note rather than an exception, so a sweep survives
  an outage but the signal quietly thins. If sweeps go unexpectedly quiet, run
  `twitter user punk6529` before concluding there is nothing to find. Trying
  `pipx upgrade twitter-cli` is worth it: these 404s are usually a stale
  private-endpoint definition, and getting `followers` back re-enables empirical
  re-seeding of the list.
- **The follow window is capped at 200 per account.** `twitter-cli` fetches one
  page and exposes no pagination, so each sweep sees a window rather than a full
  following list. `smart_graph.py` therefore diffs against the cumulative union
  of every window ever seen, not against the previous window. That is what stops
  an old follow scrolling back into view from being reported as new, and it
  keeps the diff correct without needing to know how X orders that page. The
  cost is that unfollows are invisible, which does not matter here.
- **OpenSea now returns 401 without an API key** and publishes almost no X
  handles on this chain (3 of 50 collections carried a `twitter_username`).
  It is the weakest of the three sources and nothing depends on it. Discovery
  keys on the OpenSea slug when a collection has no handle; those rows screen
  on-chain only, with no X-side evidence, and score lower for it.
- **The on-chain feed sees DeFi plumbing too.** Uniswap position receipts and
  fee-beneficiary tokens are ERC-721s that mint continuously, and they
  outnumber real drops. `INFRA_NFT_PATTERNS` filters them by contract name. If
  a real collection ever gets filtered, that list is where to look.
- **Factory-deployed contracts** return no direct deployer, so those rows get a
  `no-prior-history` flag rather than a wrong answer.
- **Mint open times** come from whatever the row says. If a project moves its
  time and nothing updates the row, the executor waits on the stale one.
