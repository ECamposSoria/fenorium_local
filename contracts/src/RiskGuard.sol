// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

import {IParOracle} from "./interfaces/IParOracle.sol";

contract RiskGuard is AccessControl {
    uint256 public constant BPS = 10_000;

    bytes32 public constant GUARDIAN_ROLE = keccak256("GUARDIAN_ROLE");
    bytes32 public constant SENTINEL_ROLE = keccak256("SENTINEL_ROLE");

    struct AssetPolicy {
        bool allowed;
        uint16 concentrationCapBps;
        uint40 maxStaleness;
        IParOracle oracle;
    }

    struct AllocationContext {
        address asset;
        uint8 job;
        uint256 assets;
        uint256 totalAssets;
        uint256 idleReserve;
        uint256 currentAssetExposure;
        uint256 currentJobExposure;
    }

    error AssetNotAllowed(address asset);
    error AssetIncident(address asset);
    error AssetConcentrationExceeded(address asset, uint256 projected, uint256 maximum);
    error JobConcentrationExceeded(uint8 job, uint256 projected, uint256 maximum);
    error ReserveFloorBreached(uint256 projectedReserve, uint256 minimumReserve);
    error OraclePriceInvalid(address asset);
    error OracleStale(address asset, uint256 updatedAt, uint256 maxStaleness);
    error CircuitBreakerActive();
    error InvalidBps(uint256 bps);
    error InvalidOracle(address oracle);

    mapping(address asset => AssetPolicy) public assetPolicies;
    mapping(uint8 job => uint16 capBps) public jobCapsBps;
    mapping(address asset => bool) public incidents;

    uint16 public minimumReserveBps;
    bool public paused;

    event AssetPolicySet(
        address indexed asset, bool allowed, uint16 concentrationCapBps, address indexed oracle, uint40 maxStaleness
    );
    event JobCapSet(uint8 indexed job, uint16 capBps);
    event MinimumReserveSet(uint16 minimumReserveBps);
    event CircuitBreakerTripped(address indexed guardian);
    event CircuitBreakerCleared(address indexed governance);
    event IncidentRaised(address indexed asset, address indexed sentinel);
    event IncidentCleared(address indexed asset, address indexed governance);

    constructor(address governance, address guardian, address sentinel) {
        _grantRole(DEFAULT_ADMIN_ROLE, governance);
        _grantRole(GUARDIAN_ROLE, guardian);
        _grantRole(SENTINEL_ROLE, sentinel);
    }

    function setAssetPolicy(
        address asset,
        bool allowed,
        uint16 concentrationCapBps,
        IParOracle oracle,
        uint40 maxStaleness
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (concentrationCapBps > BPS) revert InvalidBps(concentrationCapBps);
        if (allowed && address(oracle) == address(0)) revert InvalidOracle(address(oracle));
        assetPolicies[asset] = AssetPolicy(allowed, concentrationCapBps, maxStaleness, oracle);
        emit AssetPolicySet(asset, allowed, concentrationCapBps, address(oracle), maxStaleness);
    }

    function setJobCap(uint8 job, uint16 capBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (capBps > BPS) revert InvalidBps(capBps);
        jobCapsBps[job] = capBps;
        emit JobCapSet(job, capBps);
    }

    function setMinimumReserveBps(uint16 reserveBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (reserveBps > BPS) revert InvalidBps(reserveBps);
        minimumReserveBps = reserveBps;
        emit MinimumReserveSet(reserveBps);
    }

    function tripCircuitBreaker() external onlyRole(GUARDIAN_ROLE) {
        paused = true;
        emit CircuitBreakerTripped(msg.sender);
    }

    function clearCircuitBreaker() external onlyRole(DEFAULT_ADMIN_ROLE) {
        paused = false;
        emit CircuitBreakerCleared(msg.sender);
    }

    function raiseIncident(address asset) external onlyRole(SENTINEL_ROLE) {
        incidents[asset] = true;
        emit IncidentRaised(asset, msg.sender);
    }

    function clearIncident(address asset) external onlyRole(DEFAULT_ADMIN_ROLE) {
        incidents[asset] = false;
        emit IncidentCleared(asset, msg.sender);
    }

    function checkAllocation(AllocationContext calldata context) external view {
        if (paused) revert CircuitBreakerActive();

        AssetPolicy memory policy = assetPolicies[context.asset];
        if (!policy.allowed) revert AssetNotAllowed(context.asset);
        if (incidents[context.asset]) revert AssetIncident(context.asset);

        _checkConcentrations(
            context.asset,
            context.job,
            context.assets,
            context.totalAssets,
            policy.concentrationCapBps,
            context.currentAssetExposure,
            context.currentJobExposure
        );
        _checkReserve(context.assets, context.totalAssets, context.idleReserve);
        _checkOracle(context.asset, policy);
    }

    function _checkConcentrations(
        address asset,
        uint8 job,
        uint256 assets,
        uint256 totalAssets_,
        uint16 assetCapBps,
        uint256 currentAssetExposure,
        uint256 currentJobExposure
    ) private view {
        uint256 assetMaximum = totalAssets_ * assetCapBps / BPS;
        uint256 projectedAsset = currentAssetExposure + assets;
        if (projectedAsset > assetMaximum) {
            revert AssetConcentrationExceeded(asset, projectedAsset, assetMaximum);
        }

        uint256 jobMaximum = totalAssets_ * jobCapsBps[job] / BPS;
        uint256 projectedJob = currentJobExposure + assets;
        if (projectedJob > jobMaximum) revert JobConcentrationExceeded(job, projectedJob, jobMaximum);
    }

    function _checkReserve(uint256 assets, uint256 totalAssets_, uint256 idleReserve) private view {
        uint256 minimumReserve = totalAssets_ * minimumReserveBps / BPS;
        if (assets > idleReserve) revert ReserveFloorBreached(0, minimumReserve);
        uint256 projectedReserve = idleReserve - assets;
        if (projectedReserve < minimumReserve) revert ReserveFloorBreached(projectedReserve, minimumReserve);
    }

    function _checkOracle(address asset, AssetPolicy memory policy) private view {
        (uint256 price_, uint256 updatedAt) = policy.oracle.price(asset);
        if (price_ == 0 || updatedAt > block.timestamp) revert OraclePriceInvalid(asset);
        if (block.timestamp - updatedAt > policy.maxStaleness) {
            revert OracleStale(asset, updatedAt, policy.maxStaleness);
        }
    }
}
