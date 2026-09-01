// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {FreeMintTestNFT} from "../src/FreeMintTestNFT.sol";

contract FreeMintTestNFTTest is Test {
    FreeMintTestNFT private collection;
    address private alice = makeAddr("alice");
    address private bob = makeAddr("bob");

    function setUp() public {
        collection = new FreeMintTestNFT("Takumi Rugs", "RUGS", 5, 2, block.timestamp, "ipfs://test-root/metadata/");
    }

    function testPublicMintIsFreeAndTracksWalletLimit() public {
        vm.prank(alice);
        collection.publicMint(2);

        assertEq(collection.balanceOf(alice), 2);
        assertEq(collection.minted(alice), 2);
        assertEq(collection.totalMinted(), 2);
        assertEq(collection.ownerOf(1), alice);
        assertEq(collection.ownerOf(2), alice);
    }

    function testMintAliasIsBotCompatible() public {
        vm.prank(bob);
        collection.mint(1);

        assertEq(collection.balanceOf(bob), 1);
        assertEq(collection.mintPrice(), 0);
        assertEq(collection.maxMintPerWallet(), 2);
    }

    function testPaidMintReverts() public {
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        vm.expectRevert(FreeMintTestNFT.MintMustBeFree.selector);
        collection.publicMint{value: 1 wei}(1);
    }

    function testWalletLimitAndSupplyAreEnforced() public {
        vm.prank(alice);
        collection.publicMint(2);

        vm.prank(alice);
        vm.expectRevert(FreeMintTestNFT.WalletLimitExceeded.selector);
        collection.publicMint(1);

        vm.prank(bob);
        collection.publicMint(2);

        vm.prank(address(0xBEEF));
        vm.expectRevert(FreeMintTestNFT.SupplyExceeded.selector);
        collection.publicMint(2);
    }

    function testMintCannotOpenBeforeStart() public {
        FreeMintTestNFT futureCollection = new FreeMintTestNFT(
            "Future Takumi Rugs", "RUGS2", 5, 2, block.timestamp + 1 hours, "ipfs://test-root/metadata/"
        );

        vm.prank(alice);
        vm.expectRevert(FreeMintTestNFT.MintNotOpen.selector);
        futureCollection.publicMint(1);
    }

    function testTokenUriUsesTheHostedMemeMetadata() public {
        vm.prank(alice);
        collection.publicMint(1);

        string memory uri = collection.tokenURI(1);
        assertEq(uri, "ipfs://test-root/metadata/1.json");
        assertEq(collection.contractURI(), "ipfs://test-root/metadata/collection.json");
    }

    function testOwnerCanSetMetadataAfterUpload() public {
        vm.prank(alice);
        collection.publicMint(1);

        vm.expectRevert(FreeMintTestNFT.EmptyMetadataURI.selector);
        collection.setBaseTokenURI("");

        collection.setBaseTokenURI("ipfs://new-root/metadata/");
        assertEq(collection.baseTokenURI(), "ipfs://new-root/metadata/");
        assertEq(collection.tokenURI(1), "ipfs://new-root/metadata/1.json");
    }
}
