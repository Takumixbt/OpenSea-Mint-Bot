"""
The blockchain side. Takes verified mint instructions (from OpenSea or the
direct public SeaDrop planner), turns them into a real signed transaction, then
sends it and waits for it to land.

Uses EIP-1559 fees (the modern "base fee + tip" model that Ethereum, Base,
Optimism, Arbitrum and Polygon all support). Nothing here is specific to one
chain, so the same code works whichever chain your drop is on.

The private key is used only to sign, in memory. It is never printed or saved.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config


POA_CHAIN_IDS = {137}

# The warm-up connectivity check retries on this schedule. It runs inside the
# warm-up lead, never after the mint opens.
WARMUP_RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0)


class Minter:
    def __init__(self, rpc_url, private_key, address, chain_id, rpc_urls=None):
        self.rpc_url = str(rpc_url)
        endpoints = [self.rpc_url]
        for endpoint in rpc_urls or []:
            endpoint = str(endpoint or "").strip()
            if endpoint and endpoint not in endpoints:
                endpoints.append(endpoint)
        self.rpc_urls = endpoints
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        if int(chain_id) in POA_CHAIN_IDS:
            # Polygon includes a longer proof-of-authority extraData field.
            # web3.py requires this compatibility layer before reading blocks.
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._private_key = private_key
        self.address = Web3.to_checksum_address(address)
        self.chain_id = chain_id
        self._cached_nonce = None
        self._cached_gas_fees = None
        self.last_receipt = None
        self.last_broadcast_count = 0
        # Providers for the extra broadcast endpoints, built during warm-up so
        # no connection setup happens on the launch path.
        self._providers = {self.rpc_url: self.w3}
        self._warmed_at = None
        self._cached_balance = None
        self._balance_read_at = None

    def warm_up(self):
        """
        Do the slow lookups BEFORE the race: confirm we can reach the chain,
        confirm the chain id matches what you configured, and pre-fetch the
        wallet's next transaction number ("nonce") so we don't wait on it at
        fire time.
        """
        live_chain_id = self._connect_with_retry()
        if live_chain_id != self.chain_id:
            raise RuntimeError(
                f"Chain mismatch: config.py's TARGET_CHAIN_ID is {self.chain_id} but "
                f"the RPC actually connected to chain {live_chain_id}. Check "
                f"CHAIN_CONFIGS in config.py maps TARGET_CHAIN_ID to the right RPC."
            )
        if len(self.rpc_urls) > 1:
            # Never blast a transaction at an endpoint that reports a
            # different chain. A wrong-chain raw transaction cannot spend
            # funds, but filtering it keeps the launch path predictable.
            def endpoint_provider(url):
                provider = Web3(
                    Web3.HTTPProvider(url, request_kwargs={"timeout": 8})
                )
                # Reading the chain id both validates the endpoint and opens
                # its TLS connection, so the pool is hot before launch.
                return provider, int(provider.eth.chain_id)

            verified = {self.rpc_url}
            with ThreadPoolExecutor(
                max_workers=min(8, len(self.rpc_urls) - 1),
                thread_name_prefix="rpc-check",
            ) as pool:
                futures = {
                    pool.submit(endpoint_provider, url): url
                    for url in self.rpc_urls[1:]
                }
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        provider, endpoint_chain = future.result()
                    except Exception:
                        continue
                    if endpoint_chain == int(self.chain_id):
                        verified.add(url)
                        self._providers[url] = provider
            # Preserve the configured order after concurrent verification.
            self.rpc_urls = [url for url in self.rpc_urls if url in verified]
        # "pending" so that if this wallet somehow has a transaction already
        # waiting in the pool, we take the next number after it instead of
        # colliding with it.
        self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        # Fetch fee data during warm-up so the critical path needs only gas
        # estimation and the final balance guard after calldata arrives.
        self._cached_gas_fees = self._gas_fees(refresh=True)
        self.native_balance()
        self._warmed_at = time.monotonic()
        return live_chain_id, self._cached_nonce

    def _connect_with_retry(self):
        """Confirm the RPC is reachable, tolerating a transient blip.

        Reading the chain id both proves connectivity and opens the connection
        pool. This runs inside the warm-up lead, well before the opening, so
        the retries can never delay a broadcast.
        """
        last_error = None
        for attempt, pause in enumerate(WARMUP_RETRY_DELAYS_SECONDS, 1):
            try:
                return int(self.w3.eth.chain_id)
            except Exception as exc:
                last_error = exc
                if attempt < len(WARMUP_RETRY_DELAYS_SECONDS):
                    time.sleep(pause)
        raise RuntimeError(
            "Cannot reach the blockchain after "
            f"{len(WARMUP_RETRY_DELAYS_SECONDS)} attempts "
            f"({type(last_error).__name__}). Check ALCHEMY_API_KEY and the "
            "network in .env."
        ) from last_error

    def refresh_nonce(self):
        self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        return self._cached_nonce

    def refresh_submission_state(self, max_age_seconds=10.0):
        """Refresh nonce and fee data before the signing boundary.

        Warm-up already read the nonce and fees, and it runs inside the same
        warm-up lead as this call, so re-reading them a few seconds later costs
        three RPC round trips - about three seconds on a slow chain - and
        changes nothing. A warm-up newer than ``max_age_seconds`` is reused.
        """
        if (
            self._warmed_at is not None
            and self._cached_nonce is not None
            and self._cached_gas_fees is not None
            and time.monotonic() - self._warmed_at <= float(max_age_seconds)
        ):
            return self._cached_nonce, self._cached_gas_fees
        self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        self._cached_gas_fees = self._gas_fees(refresh=True)
        self._warmed_at = time.monotonic()
        return self._cached_nonce, self._cached_gas_fees

    def native_balance(self, max_age_seconds=0.0):
        """Return the wallet's native-coin balance without signing anything.

        ``max_age_seconds`` lets the launch path reuse a balance read taken
        during warm-up instead of paying for a round trip after the opening.
        The value is only ever used to refuse an underfunded transaction, so a
        few seconds of staleness cannot authorize a larger spend.
        """
        if (
            max_age_seconds
            and self._cached_balance is not None
            and self._balance_read_at is not None
            and time.monotonic() - self._balance_read_at <= float(max_age_seconds)
        ):
            return self._cached_balance
        self._cached_balance = self.w3.eth.get_balance(self.address)
        self._balance_read_at = time.monotonic()
        return self._cached_balance

    def peek_balance(self, timeout=6):
        """One status-only balance read. Does not warm mint RPC pools or retry."""
        provider = Web3(
            Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": timeout})
        )
        if int(self.chain_id) in POA_CHAIN_IDS:
            provider.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return int(provider.eth.get_balance(self.address))

    def funding_preview(self, mint_value_wei=0):
        """Return a read-only funding envelope; never build, sign, or send."""
        live_chain_id, _ = self.warm_up()
        balance_wei = int(self.native_balance())
        max_fee_wei, priority_fee_wei = self._gas_fees()
        mint_value_wei = int(mint_value_wei)
        if mint_value_wei < 0:
            raise ValueError("mint value cannot be negative")
        estimated_gas_wei = int(config.GAS_LIMIT_FALLBACK) * int(max_fee_wei)
        maximum_gas_wei = int(config.GAS_LIMIT_MAX) * int(
            self.w3.to_wei(config.MAX_FEE_CAP_GWEI, "gwei")
        )
        estimated_total_wei = mint_value_wei + estimated_gas_wei
        maximum_total_wei = mint_value_wei + maximum_gas_wei
        return {
            "chain_id": live_chain_id,
            "balance_wei": balance_wei,
            "mint_value_wei": mint_value_wei,
            "estimated_gas_wei": estimated_gas_wei,
            "maximum_gas_wei": maximum_gas_wei,
            "estimated_total_wei": estimated_total_wei,
            "maximum_total_wei": maximum_total_wei,
            "estimated_shortfall_wei": max(0, estimated_total_wei - balance_wei),
            "maximum_shortfall_wei": max(0, maximum_total_wei - balance_wei),
            "max_fee_per_gas_wei": int(max_fee_wei),
            "priority_fee_per_gas_wei": int(priority_fee_wei),
        }

    def _gas_fees(self, refresh=False):
        if self._cached_gas_fees is not None and not refresh:
            return self._cached_gas_fees
        # Bid a tip a bit above the current going rate to jump the queue, then
        # cap the total per the config so a spike can never overpay.
        try:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        except (KeyError, TypeError):
            base_fee = self.w3.eth.gas_price
        try:
            tip = self.w3.eth.max_priority_fee
        except Exception:
            tip = self.w3.to_wei(1.5, "gwei")

        priority_fee = int(tip * config.PRIORITY_FEE_MULTIPLIER)
        # max fee must cover base fee (doubled for headroom against a rising
        # base fee across a block or two) plus our tip.
        max_fee = base_fee * 2 + priority_fee

        cap = self.w3.to_wei(config.MAX_FEE_CAP_GWEI, "gwei")
        if max_fee > cap:
            max_fee = cap
        if priority_fee > cap:
            priority_fee = cap
        fees = (max_fee, priority_fee)
        self._cached_gas_fees = fees
        return fees

    def build_transaction(
        self,
        to,
        data,
        value,
        approved_value_wei=None,
        max_value_wei=None,
        value_label="mint",
        balance_max_age_seconds=0.0,
    ):
        """
        Build the signed-but-not-yet-sent transaction from OpenSea's calldata.
        Returns (signed_tx, human_summary_dict).
        """
        value = int(value)
        if value < 0:
            raise RuntimeError("OpenSea returned a negative mint value; refusing to build the transaction.")
        if approved_value_wei is not None and value != int(approved_value_wei):
            raise RuntimeError(
                "OpenSea returned a mint value different from the amount approved "
                f"in Telegram (approved {int(approved_value_wei)} wei; returned {value} wei). "
                "Refresh the drop and confirm the current stage again. Nothing was signed or sent."
            )
        cap_wei = config.MAX_MINT_VALUE_WEI if max_value_wei is None else int(max_value_wei)
        if value > cap_wei:
            raise RuntimeError(
                f"The {value_label} transaction asks to send {value} wei of the "
                f"chain's coin, above the configured hard cap of {cap_wei} wei. "
                "Nothing was signed or sent."
            )
        if not Web3.is_address(to):
            raise RuntimeError("the mint route returned an invalid transaction target; nothing was signed")
        if isinstance(data, bytes):
            data = Web3.to_hex(data)
        if (
            not isinstance(data, str)
            or not data.startswith("0x")
            or len(data) < 6
            or len(data[2:]) % 2
            or any(char not in "0123456789abcdefABCDEF" for char in data[2:])
        ):
            raise RuntimeError("the mint route returned invalid calldata; nothing was signed")
        if self._cached_nonce is None:
            self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")

        max_fee, priority_fee = self._gas_fees()

        tx = {
            "from": self.address,
            "to": Web3.to_checksum_address(to),
            "data": data,
            "value": value,
            "nonce": self._cached_nonce,
            "chainId": self.chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
        }

        # Estimate gas; if the node refuses (some drops revert estimation before
        # open), fall back to the configured fixed amount so we can still fire.
        try:
            estimated = self.w3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated * config.GAS_LIMIT_MULTIPLIER)
        except Exception:
            tx["gas"] = config.GAS_LIMIT_FALLBACK
        # Hard ceiling: the worst-case total fee is gas * maxFeePerGas, so
        # capping the per-gas price alone isn't enough if the estimate balloons.
        if tx["gas"] > config.GAS_LIMIT_MAX:
            tx["gas"] = config.GAS_LIMIT_MAX

        # Refuse before signing if the current balance cannot cover both the
        # mint value and this transaction's worst-case gas envelope. This is a
        # read-only balance check; no transaction exists yet.
        required_balance = tx["value"] + tx["gas"] * tx["maxFeePerGas"]
        available_balance = self.native_balance(balance_max_age_seconds)
        if available_balance < required_balance:
            required_native = self.w3.from_wei(required_balance, "ether")
            available_native = self.w3.from_wei(available_balance, "ether")
            raise RuntimeError(
                "Wallet balance is below the mint value plus the estimated gas "
                f"envelope (available {available_native} native coin; "
                f"required about {required_native}). Nothing was signed or sent."
            )

        signed = self.w3.eth.account.sign_transaction(tx, private_key=self._private_key)

        summary = {
            "to": tx["to"],
            "value_wei": tx["value"],
            "worst_case_fee_wei": tx["gas"] * tx["maxFeePerGas"],
            "nonce": tx["nonce"],
            "chainId": tx["chainId"],
            "gas": tx["gas"],
            "maxFeePerGas_gwei": round(self.w3.from_wei(max_fee, "gwei"), 3),
            "maxPriorityFeePerGas_gwei": round(self.w3.from_wei(priority_fee, "gwei"), 3),
            "data_preview": (data[:20] + "..." if isinstance(data, str) else str(data)),
        }
        return signed, summary

    def send(self, signed_tx):
        """Broadcast the signed transaction and return its transaction hash.

        When optional endpoints are configured, the exact same signed bytes
        are sent to all of them concurrently.  A duplicate raw transaction has
        the same hash, so this improves propagation without creating multiple
        mints or spending twice.
        """
        # web3 v7 renamed rawTransaction -> raw_transaction; support both so a
        # different installed web3 can't crash us at the broadcast moment.
        raw = getattr(signed_tx, "raw_transaction", None)
        if raw is None:
            raw = signed_tx.rawTransaction

        def send_one(url):
            # Warm-up already opened these connections. Building a provider
            # here would put a TLS handshake inside the launch window.
            provider = self._providers.get(url)
            if provider is None:
                provider = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
                self._providers[url] = provider
            return Web3.to_hex(provider.eth.send_raw_transaction(raw))

        if len(self.rpc_urls) == 1:
            tx_hash = send_one(self.rpc_urls[0])
            self.last_broadcast_count = 1
            return tx_hash

        successes = []
        errors = []
        with ThreadPoolExecutor(
            max_workers=min(8, len(self.rpc_urls)), thread_name_prefix="rpc-blast"
        ) as pool:
            futures = [pool.submit(send_one, url) for url in self.rpc_urls]
            for future in as_completed(futures):
                try:
                    successes.append(future.result())
                except Exception as exc:
                    errors.append(exc)
        if not successes:
            self.last_broadcast_count = 0
            detail = type(errors[0]).__name__ if errors else "unknown error"
            raise RuntimeError(f"all configured RPC endpoints rejected the transaction ({detail})")
        self.last_broadcast_count = len(successes)
        return successes[0]

    def wait_for_confirmation(self, tx_hash, timeout=180):
        """
        Poll the chain until the transaction is mined. Returns True if it
        succeeded, False if it was mined but reverted, raises on timeout.
        """
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        self.last_receipt = receipt
        return receipt["status"] == 1
