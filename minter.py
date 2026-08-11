"""
The blockchain side. Takes the mint instructions OpenSea handed us and turns
them into a real, signed transaction, then sends it and waits for it to land.

Uses EIP-1559 fees (the modern "base fee + tip" model that Ethereum, Base,
Optimism, Arbitrum and Polygon all support). Nothing here is specific to one
chain, so the same code works whichever chain your drop is on.

The private key is used only to sign, in memory. It is never printed or saved.
"""

from web3 import Web3

import config


class Minter:
    def __init__(self, rpc_url, private_key, address, chain_id):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self._private_key = private_key
        self.address = Web3.to_checksum_address(address)
        self.chain_id = chain_id
        self._cached_nonce = None

    def warm_up(self):
        """
        Do the slow lookups BEFORE the race: confirm we can reach the chain,
        confirm the chain id matches what you configured, and pre-fetch the
        wallet's next transaction number ("nonce") so we don't wait on it at
        fire time.
        """
        if not self.w3.is_connected():
            raise RuntimeError("Cannot reach the blockchain. Check ALCHEMY_API_KEY in .env.")
        live_chain_id = self.w3.eth.chain_id
        if live_chain_id != self.chain_id:
            raise RuntimeError(
                f"Chain mismatch: config.py's TARGET_CHAIN_ID is {self.chain_id} but "
                f"the RPC actually connected to chain {live_chain_id}. Check "
                f"CHAIN_RPC_SUBDOMAINS in config.py maps TARGET_CHAIN_ID to the right subdomain."
            )
        # "pending" so that if this wallet somehow has a transaction already
        # waiting in the pool, we take the next number after it instead of
        # colliding with it.
        self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        return live_chain_id, self._cached_nonce

    def refresh_nonce(self):
        self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")
        return self._cached_nonce

    def _gas_fees(self):
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
        return max_fee, priority_fee

    def build_transaction(self, to, data, value):
        """
        Build the signed-but-not-yet-sent transaction from OpenSea's calldata.
        Returns (signed_tx, human_summary_dict).
        """
        if int(value) > config.MAX_MINT_VALUE_WEI:
            raise RuntimeError(
                f"OpenSea's mint instructions ask to send {int(value)} wei of the "
                f"chain's coin, but the configured cap is "
                f"{config.MAX_MINT_PRICE_NATIVE} native coin. "
                f"Refusing to build the transaction. If this paid drop is intended, "
                f"raise MAX_MINT_PRICE_NATIVE in config.py deliberately."
            )

        if self._cached_nonce is None:
            self._cached_nonce = self.w3.eth.get_transaction_count(self.address, "pending")

        max_fee, priority_fee = self._gas_fees()

        tx = {
            "from": self.address,
            "to": Web3.to_checksum_address(to),
            "data": data,
            "value": int(value),
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

        signed = self.w3.eth.account.sign_transaction(tx, private_key=self._private_key)

        summary = {
            "to": tx["to"],
            "value_wei": tx["value"],
            "nonce": tx["nonce"],
            "chainId": tx["chainId"],
            "gas": tx["gas"],
            "maxFeePerGas_gwei": round(self.w3.from_wei(max_fee, "gwei"), 3),
            "maxPriorityFeePerGas_gwei": round(self.w3.from_wei(priority_fee, "gwei"), 3),
            "data_preview": (data[:20] + "..." if isinstance(data, str) else str(data)),
        }
        return signed, summary

    def send(self, signed_tx):
        """Broadcast the signed transaction. Returns the transaction hash."""
        # web3 v7 renamed rawTransaction -> raw_transaction; support both so a
        # different installed web3 can't crash us at the broadcast moment.
        raw = getattr(signed_tx, "raw_transaction", None)
        if raw is None:
            raw = signed_tx.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        return Web3.to_hex(tx_hash)

    def wait_for_confirmation(self, tx_hash, timeout=180):
        """
        Poll the chain until the transaction is mined. Returns True if it
        succeeded, False if it was mined but reverted, raises on timeout.
        """
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        return receipt["status"] == 1
