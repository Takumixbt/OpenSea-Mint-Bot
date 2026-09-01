// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {FreeMintTestNFT} from "../src/FreeMintTestNFT.sol";

contract Deploy is Script {
    function run() external returns (FreeMintTestNFT collection) {
        uint256 deployerPrivateKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        uint256 maxSupply = vm.envOr("MAX_SUPPLY", uint256(50));
        uint256 maxPerWallet = vm.envOr("MAX_PER_WALLET", uint256(2));
        uint256 explicitStart = vm.envOr("MINT_START_TIMESTAMP", uint256(0));
        uint256 startDelay = vm.envOr("MINT_START_DELAY_SECONDS", uint256(120));
        string memory baseTokenURI = vm.envOr("BASE_TOKEN_URI", string(""));
        uint256 startTime = explicitStart == 0 ? block.timestamp + startDelay : explicitStart;

        vm.startBroadcast(deployerPrivateKey);
        collection = new FreeMintTestNFT("Takumi Rugs", "RUGS", maxSupply, maxPerWallet, startTime, baseTokenURI);
        vm.stopBroadcast();
    }
}
