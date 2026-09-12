# Roadmap

## Arc

OpenSea has said they will support [Arc](https://www.arc.network/) (Circle's
USDC-gas L1) from day one of public mainnet. This bot does **not** ship an Arc
signer yet.

Why it is a roadmap item instead of a chain row:

- Circle has not published a stable public mainnet chain ID, RPC, or explorer
  in their own docs. Third-party lists currently disagree (`5042` vs `1243`).
- Gas is **USDC**, not ETH. Every cap, fee envelope, and `native` label in
  this repo assumes a chain-native coin. Wiring Arc as if it were another ETH
  L2 would lie on every screen.
- The OpenSea chain slug is not in this repo's `CHAIN_CONFIGS` until OpenSea
  lists it on `/chains`.

When those three facts are public:

1. Add `arc` to `CHAIN_CONFIGS` with the official chain ID, RPC, USDC symbol,
   explorer, and logo.
2. Treat native-USDC fees as a first-class case (caps in USDC, never call it
   ETH).
3. Confirm the OpenSea slug from their `/chains` response. Do not guess.

Arc **testnet** (`5042002`, `https://rpc.testnet.arc.io`, USDC, explorer
`testnet.arcscan.app`) can be used to prove the signer path before mainnet.
It will not mint OpenSea mainnet drops.

## Not in scope

- Solana / non-EVM signers
- Guessing calldata for custom mint contracts
- Hosting keys for other people
