// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

import {RiskGuard} from "./RiskGuard.sol";
import {IParAdapter} from "./adapters/IParAdapter.sol";

contract ParVault is ERC4626, AccessControl, ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    uint256 public constant BPS = 10_000;
    bytes32 public constant ALLOCATOR_ROLE = keccak256("ALLOCATOR_ROLE");
    bytes32 public constant IMMEDIATE_EXIT_TYPEHASH = keccak256(
        "ImmediateExit(address owner,uint256 shares,uint256 unwindCostAssets,uint256 deadline,address adapter,bytes32 dataHash,uint256 nonce)"
    );

    enum Job {
        LEND_FIXED,
        DISPERSION,
        CARRY,
        RESERVE
    }

    struct ScheduledExit {
        address owner;
        uint256 shares;
        bool claimed;
    }

    struct ImmediateExitQuote {
        address owner;
        uint256 shares;
        uint256 unwindCostAssets;
        uint256 deadline;
        address adapter;
        bytes32 dataHash;
        uint256 nonce;
    }

    RiskGuard public immutable riskGuard;
    uint16 public maximumUnwindCostBps;

    mapping(address account => bool) public whitelisted;
    mapping(address account => uint256) public immediateExitNonces;
    mapping(address adapter => bool) public registeredAdapters;
    mapping(address adapter => bool) public activeAdapters;
    mapping(address adapter => Job) public adapterJob;
    mapping(address adapter => bool) public adapterHasJob;
    IParAdapter[] private _adapters;

    mapping(uint256 id => ScheduledExit) public scheduledExits;
    uint256 public scheduledExitCount;
    uint256 public nextScheduledExitId;

    error AdapterNotActive(address adapter);
    error AdapterJobMismatch(address adapter, Job expected, Job actual);
    error AdapterInvestmentMismatch(uint256 requested, uint256 invested);
    error InvalidBps(uint256 bps);
    error InvalidAddress();
    error InvalidAmount();
    error QuoteExpired(uint256 deadline);
    error UnwindCostTooHigh(uint256 quoted, uint256 maximum);
    error UnwindQuoteInsufficient(uint256 quoted, uint256 realizedLoss);
    error InvalidQuoteSigner(address signer);
    error InvalidQuoteNonce(uint256 supplied, uint256 expected);
    error InsufficientReserve(uint256 available, uint256 required);
    error ScheduledExitNotNext(uint256 requested, uint256 next);
    error ScheduledExitAlreadyClaimed(uint256 id);

    event WhitelistSet(address indexed account, bool allowed);
    event AdapterSet(address indexed adapter, address indexed asset, bool active);
    event Allocated(Job indexed job, address indexed adapter, uint256 assets);
    event Divested(address indexed adapter, uint256 positionAssets, uint256 assetsOut);
    event MaturedRedeemed(address indexed adapter, uint256 assetsOut);
    event MaximumUnwindCostSet(uint16 maximumUnwindCostBps);
    event ScheduledExitRequested(uint256 indexed id, address indexed owner, uint256 shares);
    event ScheduledExitClaimed(uint256 indexed id, address indexed owner, uint256 shares, uint256 assets);
    event ImmediateRedeemed(
        address indexed owner,
        uint256 shares,
        uint256 grossAssets,
        uint256 unwindCostAssets,
        uint256 realizedLoss,
        uint256 payout
    );

    constructor(
        IERC20 asset_,
        string memory name_,
        string memory symbol_,
        RiskGuard riskGuard_,
        address governance,
        address allocator
    ) ERC20(name_, symbol_) ERC4626(asset_) EIP712(name_, "1") {
        if (address(asset_) == address(0) || address(riskGuard_) == address(0) || governance == address(0)) {
            revert InvalidAddress();
        }
        riskGuard = riskGuard_;
        _grantRole(DEFAULT_ADMIN_ROLE, governance);
        _grantRole(ALLOCATOR_ROLE, allocator);
    }

    function maxDeposit(address receiver) public view override returns (uint256) {
        return whitelisted[receiver] ? super.maxDeposit(receiver) : 0;
    }

    function maxMint(address receiver) public view override returns (uint256) {
        return whitelisted[receiver] ? super.maxMint(receiver) : 0;
    }

    function maxWithdraw(address) public pure override returns (uint256) {
        return 0;
    }

    function maxRedeem(address) public pure override returns (uint256) {
        return 0;
    }

    function totalAssets() public view override returns (uint256 assets_) {
        assets_ = IERC20(asset()).balanceOf(address(this));
        uint256 length = _adapters.length;
        for (uint256 i; i < length; ++i) {
            assets_ += _adapters[i].positionValue();
        }
    }

    function idleReserve() public view returns (uint256) {
        return IERC20(asset()).balanceOf(address(this));
    }

    function adapters() external view returns (IParAdapter[] memory) {
        return _adapters;
    }

    function setWhitelist(address account, bool allowed) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (account == address(0)) revert InvalidAddress();
        whitelisted[account] = allowed;
        emit WhitelistSet(account, allowed);
    }

    function setAdapter(IParAdapter adapter, bool active) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (address(adapter) == address(0)) revert InvalidAddress();
        if (!registeredAdapters[address(adapter)]) {
            registeredAdapters[address(adapter)] = true;
            _adapters.push(adapter);
        }
        activeAdapters[address(adapter)] = active;
        emit AdapterSet(address(adapter), adapter.asset(), active);
    }

    function setMaximumUnwindCostBps(uint16 maximumBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (maximumBps > BPS) revert InvalidBps(maximumBps);
        maximumUnwindCostBps = maximumBps;
        emit MaximumUnwindCostSet(maximumBps);
    }

    function assetExposure(address instrument) public view returns (uint256 exposure) {
        uint256 length = _adapters.length;
        for (uint256 i; i < length; ++i) {
            if (_adapters[i].asset() == instrument) exposure += _adapters[i].positionValue();
        }
    }

    function jobExposure(Job job) public view returns (uint256 exposure) {
        uint256 length = _adapters.length;
        for (uint256 i; i < length; ++i) {
            address adapter = address(_adapters[i]);
            if (adapterHasJob[adapter] && adapterJob[adapter] == job) exposure += _adapters[i].positionValue();
        }
    }

    function allocate(Job job, address adapter, uint256 assets, bytes calldata data)
        external
        onlyRole(ALLOCATOR_ROLE)
        nonReentrant
    {
        if (!activeAdapters[adapter]) revert AdapterNotActive(adapter);
        if (assets == 0) revert InvalidAmount();
        if (adapterHasJob[adapter] && adapterJob[adapter] != job) {
            revert AdapterJobMismatch(adapter, adapterJob[adapter], job);
        }

        IParAdapter target = IParAdapter(adapter);
        address instrument = target.asset();
        riskGuard.checkAllocation(
            RiskGuard.AllocationContext({
                asset: instrument,
                job: uint8(job),
                assets: assets,
                totalAssets: totalAssets(),
                idleReserve: idleReserve(),
                currentAssetExposure: assetExposure(instrument),
                currentJobExposure: jobExposure(job)
            })
        );

        adapterHasJob[adapter] = true;
        adapterJob[adapter] = job;
        IERC20(asset()).safeTransfer(adapter, assets);
        uint256 invested = target.invest(assets, data);
        if (invested != assets) revert AdapterInvestmentMismatch(assets, invested);
        emit Allocated(job, adapter, assets);
    }

    function divest(address adapter, uint256 assets, bytes calldata data)
        external
        onlyRole(ALLOCATOR_ROLE)
        nonReentrant
        returns (uint256 assetsOut)
    {
        if (!registeredAdapters[adapter]) revert AdapterNotActive(adapter);
        if (assets == 0) revert InvalidAmount();
        assetsOut = IParAdapter(adapter).divest(assets, data);
        emit Divested(adapter, assets, assetsOut);
    }

    function redeemMatured(address adapter, bytes calldata data)
        external
        onlyRole(ALLOCATOR_ROLE)
        nonReentrant
        returns (uint256 assetsOut)
    {
        if (!registeredAdapters[adapter]) revert AdapterNotActive(adapter);
        assetsOut = IParAdapter(adapter).redeemMatured(data);
        emit MaturedRedeemed(adapter, assetsOut);
    }

    function requestScheduledExit(uint256 shares) external nonReentrant returns (uint256 id) {
        if (shares == 0) revert InvalidAmount();
        id = scheduledExitCount++;
        scheduledExits[id] = ScheduledExit(msg.sender, shares, false);
        _transfer(msg.sender, address(this), shares);
        emit ScheduledExitRequested(id, msg.sender, shares);
    }

    function claimScheduledExit(uint256 id) external nonReentrant returns (uint256 assets) {
        if (id != nextScheduledExitId) revert ScheduledExitNotNext(id, nextScheduledExitId);
        ScheduledExit storage exit_ = scheduledExits[id];
        if (exit_.claimed) revert ScheduledExitAlreadyClaimed(id);

        assets = previewRedeem(exit_.shares);
        uint256 reserve = idleReserve();
        if (reserve < assets) revert InsufficientReserve(reserve, assets);

        exit_.claimed = true;
        ++nextScheduledExitId;
        _burn(address(this), exit_.shares);
        IERC20(asset()).safeTransfer(exit_.owner, assets);
        emit ScheduledExitClaimed(id, exit_.owner, exit_.shares, assets);
    }

    function redeemImmediate(
        uint256 shares,
        uint256 unwindCostAssets,
        uint256 deadline,
        address adapter,
        bytes calldata data,
        uint256 nonce,
        bytes calldata allocatorSignature
    ) external nonReentrant returns (uint256 payout) {
        if (shares == 0) revert InvalidAmount();
        if (block.timestamp > deadline) revert QuoteExpired(deadline);

        ImmediateExitQuote memory quote = ImmediateExitQuote({
            owner: msg.sender,
            shares: shares,
            unwindCostAssets: unwindCostAssets,
            deadline: deadline,
            adapter: adapter,
            dataHash: keccak256(data),
            nonce: nonce
        });
        uint256 grossAssets;
        (grossAssets, payout) = _validateImmediateQuote(quote, allocatorSignature);
        _burn(msg.sender, shares);

        uint256 realizedLoss = _coverImmediateExit(grossAssets, adapter, data);

        if (realizedLoss > unwindCostAssets) revert UnwindQuoteInsufficient(unwindCostAssets, realizedLoss);
        uint256 reserve = idleReserve();
        if (reserve < payout) revert InsufficientReserve(reserve, payout);
        IERC20(asset()).safeTransfer(msg.sender, payout);
        _emitImmediateRedeemed(quote, grossAssets, realizedLoss, payout);
    }

    function _validateImmediateQuote(ImmediateExitQuote memory quote, bytes calldata allocatorSignature)
        private
        returns (uint256 grossAssets, uint256 payout)
    {
        // previewRedeem rounds down, so fractional assets remain with the vault rather than the leaver.
        grossAssets = previewRedeem(quote.shares);
        uint256 maximumCost = grossAssets * maximumUnwindCostBps / BPS;
        if (quote.unwindCostAssets > maximumCost) revert UnwindCostTooHigh(quote.unwindCostAssets, maximumCost);
        uint256 expectedNonce = immediateExitNonces[msg.sender];
        if (quote.nonce != expectedNonce) revert InvalidQuoteNonce(quote.nonce, expectedNonce);
        address signer = ECDSA.recover(immediateExitDigest(quote), allocatorSignature);
        if (!hasRole(ALLOCATOR_ROLE, signer)) revert InvalidQuoteSigner(signer);
        immediateExitNonces[msg.sender] = expectedNonce + 1;
        payout = grossAssets - quote.unwindCostAssets;
    }

    function _coverImmediateExit(uint256 grossAssets, address adapter, bytes calldata data)
        private
        returns (uint256 realizedLoss)
    {
        uint256 reserve = idleReserve();
        if (reserve >= grossAssets) return 0;
        if (!registeredAdapters[adapter]) revert AdapterNotActive(adapter);

        uint256 positionBefore = IParAdapter(adapter).positionValue();
        uint256 assetsOut = IParAdapter(adapter).divest(grossAssets - reserve, data);
        uint256 positionReduction = positionBefore - IParAdapter(adapter).positionValue();
        realizedLoss = positionReduction > assetsOut ? positionReduction - assetsOut : 0;
        emit Divested(adapter, positionReduction, assetsOut);
    }

    function _emitImmediateRedeemed(
        ImmediateExitQuote memory quote,
        uint256 grossAssets,
        uint256 realizedLoss,
        uint256 payout
    ) private {
        emit ImmediateRedeemed(quote.owner, quote.shares, grossAssets, quote.unwindCostAssets, realizedLoss, payout);
    }

    function immediateExitDigest(ImmediateExitQuote memory quote) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    IMMEDIATE_EXIT_TYPEHASH,
                    quote.owner,
                    quote.shares,
                    quote.unwindCostAssets,
                    quote.deadline,
                    quote.adapter,
                    quote.dataHash,
                    quote.nonce
                )
            )
        );
    }

    // Six virtual share decimals make a first-deposit donation attack economically prohibitive for 6-decimal USDC.
    function _decimalsOffset() internal pure override returns (uint8) {
        return 6;
    }
}
