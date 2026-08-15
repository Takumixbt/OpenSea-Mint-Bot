"""Shared low-latency mint execution used by Telegram and the CLI."""

from concurrent.futures import ThreadPoolExecutor
import time

import config
import external_mint
import opensea_client
import opensea_direct_executor
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
        scheduled_at=None,
    ):
        """Build, sign, and broadcast one explicitly confirmed candidate mint."""
        prepared_at = time.time()
        quantity = quantity or config.MINT_QUANTITY
        chain_id = int(candidate["chain_id"])
        rpc_url = config.rpc_url_for_chain(self.alchemy_key, chain_id)
        rpc_urls = config.rpc_urls_for_chain(self.alchemy_key, chain_id)
        minter = Minter(
            rpc_url,
            self.private_key,
            self.wallet_address,
            chain_id,
            rpc_urls=rpc_urls,
        )
        route = str(candidate.get("route") or "opensea_drop")
        direct_eligible = route == "opensea_drop" and opensea_direct_executor.is_candidate_eligible(candidate)
        direct_plan = None

        # RPC warm-up and the direct SeaDrop read are independent.  When the
        # direct plan is available, all calldata-dependent work can complete
        # before the opening second.  A missing/mismatched plan simply falls
        # back to the existing OpenSea route below.
        def warm_rpc():
            live_chain, nonce = minter.warm_up()
            return live_chain, nonce, minter.native_balance()

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="mint-warmup") as pool:
            rpc_future = pool.submit(warm_rpc)
            direct_future = (
                pool.submit(
                    opensea_direct_executor.build_public_plan,
                    rpc_url,
                    candidate,
                    quantity,
                )
                if direct_eligible else None
            )
            live_chain, nonce, balance = rpc_future.result()
            if direct_future is not None:
                try:
                    direct_plan = direct_future.result()
                except opensea_direct_executor.DirectSafetyError:
                    # The chain has given us a deterministic reason that this
                    # quantity would revert. Do not downgrade that to an
                    # OpenSea fallback which could still broadcast a bad tx.
                    raise
                except Exception:
                    # A direct route is an optimization, not a reason to
                    # bypass the normal verified OpenSea calldata path.
                    direct_plan = None
        if live_chain != chain_id:
            raise RuntimeError(f"RPC chain mismatch: got {live_chain}, expected {chain_id}")
        if balance <= 0:
            raise RuntimeError("wallet has no native coin for gas")

        execution_route = "direct_seadrop" if direct_plan else route
        if direct_plan:
            calldata = direct_plan
            # The direct plan was read from the chain, so its exact value is
            # the approved amount.  Minter still enforces MAX_MINT_VALUE_WEI.
            approved_value = int(calldata["value"])
            launch_at = max(
                float(scheduled_at or 0),
                float(candidate.get("start_time") or 0),
                float(calldata.get("start_time") or 0),
            )
            request_started_at = time.time()
            signed, summary = minter.build_transaction(
                calldata["to"],
                calldata["data"],
                calldata["value"],
                approved_value_wei=approved_value,
            )
        else:
            client = (
                opensea_client.get_api_client(self.opensea_api_key)
                if route == "opensea_drop" else None
            )
            try:
                # The API/TLS warm-up remains in the fallback path.  It is not
                # needed for a direct SeaDrop transaction.
                if client is not None:
                    opensea_client.prewarm_drop_route(
                        client,
                        candidate["slug"],
                        self.opensea_api_key,
                    )
                launch_at = float(scheduled_at or 0)
                if launch_at > time.time():
                    self._wait_until(launch_at)
                request_started_at = time.time()
                calldata = self._request_calldata(
                    client,
                    candidate,
                    quantity,
                    rpc_url,
                )
            finally:
                if client is not None:
                    client.close()

            signed, summary = minter.build_transaction(
                calldata["to"],
                calldata["data"],
                calldata["value"],
                approved_value_wei=self._approved_value(candidate, quantity),
            )

        # Direct SeaDrop signs before the opening; the fallback signs after
        # OpenSea provides its ready-to-sign calldata.  Both paths share the
        # same exact launch, broadcast, receipt, and result bookkeeping.
        if launch_at > time.time():
            self._wait_until(launch_at)
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
            "prepared_at": prepared_at,
            "request_started_at": request_started_at,
            "execution_route": execution_route,
            "broadcast_rpc_count": 0,
        }
        tx_hash = minter.send(signed)
        result["broadcast_rpc_count"] = minter.last_broadcast_count or 1
        result["broadcast_at"] = time.time()
        if launch_at > 0:
            result["launch_delay_ms"] = max(
                0, round((result["broadcast_at"] - launch_at) * 1000)
            )
        result["tx_hash"] = tx_hash
        result["status"] = "sent"
        try:
            result["confirmed"] = minter.wait_for_confirmation(tx_hash)
            receipt = minter.last_receipt or {}
            gas_used = int(receipt.get("gasUsed") or 0)
            effective_gas_price = int(receipt.get("effectiveGasPrice") or 0)
            result["actual_gas_wei"] = gas_used * effective_gas_price
            result["gas_used"] = gas_used
            result["block_number"] = receipt.get("blockNumber")
        except Exception as exc:
            # The transaction hash is authoritative once send() returns. A
            # receipt timeout must not erase it or make a retry look safe.
            result["confirmed"] = None
            result["confirmation_error"] = f"{type(exc).__name__}: confirmation timed out"
        return result

    def _request_calldata(self, client, candidate, quantity, rpc_url=None):
        route = str(candidate.get("route") or "opensea_drop")
        if route == "generic_contract":
            deadline = max(int(candidate.get("start_time") or 0), time.time()) + config.FIRE_TIMEOUT_SECONDS
            last_error = None
            for attempt in range(config.FIRE_MAX_ATTEMPTS):
                try:
                    return external_mint.build_generic_calldata(
                        candidate,
                        rpc_url,
                        self.wallet_address,
                        quantity,
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 >= config.FIRE_MAX_ATTEMPTS or time.time() >= deadline:
                        break
                    delays = config.FIRE_RETRY_DELAYS_SECONDS
                    time.sleep(min(delays[min(attempt, len(delays) - 1)], max(0, deadline - time.time())))
            raise RuntimeError(
                "external mint transaction did not pass launch simulation: "
                f"{type(last_error).__name__ if last_error else 'unknown error'}"
            ) from last_error
        if route != "opensea_drop":
            raise RuntimeError("this mint route needs a dedicated adapter")
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
            if attempts >= config.FIRE_MAX_ATTEMPTS:
                break
            delays = config.FIRE_RETRY_DELAYS_SECONDS
            delay = delays[min(attempts - 1, len(delays) - 1)]
            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(min(delay, remaining))
        raise RuntimeError("no usable mint transaction arrived before the fire timeout")

    @staticmethod
    def _wait_until(target_epoch):
        """Wait accurately without busy-spinning for the whole warm-up window."""
        while True:
            remaining = float(target_epoch) - time.time()
            if remaining <= 0:
                return
            if remaining > 0.10:
                time.sleep(max(0.001, remaining - 0.05))
            else:
                time.sleep(min(0.005, remaining))

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
