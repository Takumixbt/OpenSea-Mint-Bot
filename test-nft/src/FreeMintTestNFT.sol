// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

/// @title Takumi Rugs
/// @notice A deliberately small, public, zero-price ERC-721 used to exercise
///         a mint bot against an OpenSea-indexed contract.
///
/// The publicMint(uint256) shape and the zero-argument getters are intentional:
/// the bot in the parent project can discover this route from a verified ABI,
/// simulate it, and confirm that the mint price is exactly zero.
contract FreeMintTestNFT is ERC721, Ownable {
    using Strings for uint256;

    error InvalidCollectionLimits();
    error InvalidQuantity();
    error MintNotOpen();
    error SupplyExceeded();
    error WalletLimitExceeded();
    error MintMustBeFree();
    error MetadataNotConfigured();
    error EmptyMetadataURI();

    event BaseTokenURIUpdated(string newBaseTokenURI);

    uint256 public immutable maxSupply;
    uint256 public immutable maxMintPerWallet;
    uint256 public immutable publicMintStartTime;
    uint256 public constant mintPrice = 0;

    uint256 public totalMinted;
    mapping(address wallet => uint256 quantity) public minted;
    string public baseTokenURI;

    constructor(
        string memory name_,
        string memory symbol_,
        uint256 maxSupply_,
        uint256 maxMintPerWallet_,
        uint256 publicMintStartTime_,
        string memory baseTokenURI_
    ) ERC721(name_, symbol_) Ownable(msg.sender) {
        if (maxSupply_ == 0 || maxMintPerWallet_ == 0 || maxMintPerWallet_ > maxSupply_) {
            revert InvalidCollectionLimits();
        }

        maxSupply = maxSupply_;
        maxMintPerWallet = maxMintPerWallet_;
        publicMintStartTime = publicMintStartTime_;
        baseTokenURI = baseTokenURI_;
    }

    /// @notice The bot's preferred entry point. It costs zero mint value; the
    /// caller still pays the chain's normal transaction gas.
    function publicMint(uint256 quantity) external payable {
        _freeMint(quantity);
    }

    /// @notice Common alias for tools that look for a function named `mint`.
    function mint(uint256 quantity) external payable {
        _freeMint(quantity);
    }

    function _freeMint(uint256 quantity) internal {
        if (msg.value != mintPrice) revert MintMustBeFree();
        if (block.timestamp < publicMintStartTime) revert MintNotOpen();
        if (quantity == 0) revert InvalidQuantity();
        if (quantity > maxMintPerWallet || minted[msg.sender] + quantity > maxMintPerWallet) {
            revert WalletLimitExceeded();
        }
        if (totalMinted + quantity > maxSupply) revert SupplyExceeded();

        minted[msg.sender] += quantity;
        for (uint256 i = 0; i < quantity; ++i) {
            ++totalMinted;
            _safeMint(msg.sender, totalMinted);
        }
    }

    /// @notice Returns the metadata JSON hosted under the configured IPFS/base
    /// URI. The image referenced by each JSON file is one of the selected
    /// meme images prepared from the user's local folder.
    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);
        if (bytes(baseTokenURI).length == 0) revert MetadataNotConfigured();
        return string.concat(baseTokenURI, tokenId.toString(), ".json");
    }

    /// @notice OpenSea-compatible collection metadata JSON URL.
    function contractURI() external view returns (string memory) {
        if (bytes(baseTokenURI).length == 0) revert MetadataNotConfigured();
        return string.concat(baseTokenURI, "collection.json");
    }

    /// @notice Set the immutable-hosted metadata root after the IPFS bundle is
    /// uploaded. This is the only metadata mutation available to the owner.
    function setBaseTokenURI(string calldata newBaseTokenURI) external onlyOwner {
        if (bytes(newBaseTokenURI).length == 0) revert EmptyMetadataURI();
        baseTokenURI = newBaseTokenURI;
        emit BaseTokenURIUpdated(newBaseTokenURI);
    }
}
