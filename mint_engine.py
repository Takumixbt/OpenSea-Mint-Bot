"""Shared mint execution used by the CLI and Telegram daily runner."""

import time

import config
import opensea_client
from minter import Minter


class MintEngine:
    def __init__(self, alchemy_key, private_key, wallet_address, opensea_api_key):
        self.alchemy_key = alchemy_key
        self.private_key = private_key
        self.wallet_address = wallet_address
        self.opensea_api_key = opensea_api_key

    def execute(
        self,
        candidate,
        daily_gas_used_wei=0,
        daily_gas_cap_wei=None,
        quantity=None,
    ):
        """Build, sign, and broadcast one explicitly confirmed candidate mint."""
        chain_id = int(candidate["chain_id"])
        rpc_url = config.rpc_url_for_chain(self.alchemy_key, chain_id)
        minter = Minter(rpc_url, self.private_key, self.wallet_address, chain_id)
        live_chain, nonce = minter.warm_up()
        if live_chain != chain_id:
            raise RuntimeError(f"RPC chain mismatch: got {live_chain}, expected {chain_id}")
        if minter.native_balance() <= 0:
            raise RuntimeError("wallet has no native coin for gas")

        client = opensea_client.get_api_client(self.opensea_api_key)
        try:
            calldata = self._request_calldata(client, candidate, quantity or config.MINT_QUANTITY)
        finally:
            client.close()

        signed, summary = minter.build_transaction(
            calldata["to"],
            calldata["data"],
            calldata["value"],
            approved_value_wei=self._approved_value(candidate, quantity or config.MINT_QUANTITY),
        )
        worst_case_gas = int(summary.get("worst_case_fee_wei", 0))
        daily_gas_cap_wei = (
            config.MAX_DAILY_GAS_WEI if daily_gas_cap_wei is None else daily_gas_cap_wei
        )
        if daily_gas_cap_wei > 0 and daily_gas_used_wei + worst_case_gas > daily_gas_cap_wei:
            raise RuntimeError(
                "daily gas cap would be exceeded; nothing was broadcast "
                f"(used {daily_gas_used_wei} wei, next about {worst_case_gas} wei)"
            )

        result = {
            "candidate": candidate,
            "nonce": nonce,
            "summary": summary,
            "worst_case_gas_wei": worst_case_gas,
        }
        tx_hash = minter.send(signed)
        result["tx_hash"] = tx_hash
        result["status"] = "sent"
        try:
            result["confirmed"] = minter.wait_for_confirmation(tx_hash)
        except Exception as exc:
            # The transaction hash is authoritative once send() returns. A
            # receipt timeout must not erase it or make a retry look safe.
            result["confirmed"] = None
            result["confirmation_error"] = f"{type(exc).__name__}: confirmation timed out"
        return result

    def _request_calldata(self, client, candidate, quantity):
        start_time = int(candidate.get("start_time") or 0)
        deadline = max(start_time, time.time()) + config.FIRE_TIMEOUT_SECONDS
        attempts = 0
        last_note = None
        while time.time() < deadline and attempts < config.FIRE_MAX_ATTEMPTS:
            attempts += 1
            calldata, note = opensea_client.get_mint_calldata(
                client,
                candidate["slug"],
                candidate["stage_index"],
                quantity,
                self.wallet_address,
                self.opensea_api_key,
            )
            if calldata:
                return calldata
            if note and note.startswith("STOP:"):
                raise RuntimeError(note[5:].strip())
            if note != last_note:
                last_note = note
            time.sleep(config.FIRE_RETRY_SECONDS)
        raise RuntimeError("no usable mint transaction arrived before the fire timeout")

    @staticmethod
    def _approved_value(candidate, quantity):
        """Return the exact total mint value approved by the Telegram preview."""
        value = candidate.get("price_wei")
        if value is None:
            raise RuntimeError(
                "mint price is unknown; refresh the drop before a live transaction"
            )
        try:
            value = int(value)
            quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("mint price or quantity is invalid") from exc
        if value < 0 or quantity < 1:
            raise RuntimeError("mint price or quantity is invalid")
        return value * quantity
