# Roadmap

## Arc

Shipped in this build. OpenSea slug `arc` (chain ID **5042**, USDC-native gas,
explorer `https://explorer.arc.io`). Gas caps, wallet balances, and fee
envelopes on Arc are labeled **USDC**, not ETH.

Arc **testnet** (`5042002`, `https://rpc.testnet.arc.io`, USDC, explorer
`testnet.arcscan.app`) is not wired in `CHAIN_CONFIGS`. It can prove the signer
path before mainnet but will not mint OpenSea mainnet Arc drops.

## Not in scope

- Solana / non-EVM signers
- Guessing calldata for custom mint contracts
- Hosting keys for other people
