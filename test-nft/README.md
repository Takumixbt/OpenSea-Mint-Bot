# Takumi Rugs

This is a deliberately small ERC-721 collection named **Takumi Rugs** for
testing the parent OpenSea mint bot. It is not an investment project or a
production drop.

The contract has:

- `publicMint(uint256)` as the primary bot route;
- `mint(uint256)` as a compatibility alias;
- `mintPrice() == 0`, so the mint value is always zero;
- a default supply of 50 and a default limit of 2 per wallet;
- a configurable launch timestamp, so the bot can test a scheduled opening;
- token-specific JSON metadata that points to randomly selected meme images;
- an owner-settable IPFS base URI, so the same contract can be deployed before
  or after the image bundle is pinned.

## Prepare the meme artwork

The source folder is never modified. This command freezes a reproducible random
selection of 50 images into `upload/assets/` and creates matching metadata in
`upload/metadata/`:

```powershell
python scripts/prepare_memes.py `
  --source "C:\path\to\your\memes" `
  --count 50 `
  --seed 20260815 `
  --clean
```

Upload `upload/assets/` first to a pinning service such as Pinata, or to a
properly pinned IPFS node. Copy the returned asset CID, then regenerate the
metadata with that CID:

```powershell
python scripts/prepare_memes.py `
  --source "C:\path\to\your\memes" `
  --count 50 `
  --seed 20260815 `
  --assets-cid <ASSET_CID> `
  --clean
```

Now upload only `upload/metadata/` and copy its metadata CID. Set the contract
base URI to:

```text
BASE_TOKEN_URI=ipfs://<METADATA_CID>/
```

Only publish images you have permission to use. A local OneDrive path is not a
public NFT image URL; OpenSea needs the files pinned or hosted publicly.

OpenSea is a marketplace/indexer, not the blockchain itself. To make this
collection visible on the live OpenSea marketplace, deploy it to a supported
mainnet such as Base, set the IPFS base URI, wait for the first mint to be
indexed, and open the collection or an asset page from the contract address.
Deployment and each free mint still require the chain's gas token.

## Verify locally

From this directory:

```powershell
forge build
forge test -vv
```

## Deploy to Base

Copy `.env.deploy.example` to a local, untracked file and fill in a dedicated
deployer wallet and RPC URL. Do not reuse a wallet that holds meaningful funds
and do not paste a private key into Telegram or the repository.

```powershell
Copy-Item .env.deploy.example .env.deploy
$env:RPC_URL = "https://mainnet.base.org"
$env:DEPLOYER_PRIVATE_KEY = "0x..."
$env:BASE_TOKEN_URI = "ipfs://<METADATA_CID>/"

forge script script/Deploy.s.sol:Deploy `
  --rpc-url $env:RPC_URL `
  --broadcast `
  -vvvv
```

The script waits 120 seconds by default before opening `publicMint`. Set
`MINT_START_TIMESTAMP` to an explicit Unix timestamp or change
`MINT_START_DELAY_SECONDS` before deploying.

If the contract was deployed before the IPFS bundle was ready, set the URI
afterward with the owner wallet:

```powershell
cast send <CONTRACT_ADDRESS> "setBaseTokenURI(string)" $env:BASE_TOKEN_URI `
  --rpc-url $env:RPC_URL `
  --private-key $env:DEPLOYER_PRIVATE_KEY
```

For the bot to resolve the generic mint route safely, verify the contract
source on BaseScan or Sourcify after deployment. The compiled artifact is at:

```text
out/FreeMintTestNFT.sol/FreeMintTestNFT.json
```

## Smoke-test one mint

After the launch timestamp, use a dedicated test wallet:

```powershell
cast send <CONTRACT_ADDRESS> "publicMint(uint256)" 1 `
  --rpc-url $env:RPC_URL `
  --private-key $env:DEPLOYER_PRIVATE_KEY `
  --value 0
```

The contract address and token 1 can be viewed with the direct OpenSea asset
URL:

```text
https://opensea.io/assets/base/<CONTRACT_ADDRESS>/1
```

Once OpenSea has indexed it, copy the collection URL from that asset page and
use that URL with the parent bot. Keep the bot configured with:

```text
TARGET_CHAIN_ID=8453
MAX_MINT_PRICE_NATIVE=0
MINT_QUANTITY=1
```

The parent bot should report `External public mint`, `Verified contract`, and
`Free` after it can read the collection and verified ABI.
